"""Scheduled future-window inference and atomic tenant forecast publication."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
from typing import Any, Protocol

from src.models.contracts import validate_contract
from src.models.data import parse_utc
from src.models.errors import DataContractError
from src.models.operational import ForecastService


PredictionKey = tuple[str, str, str, str]


def _key(item: dict[str, Any]) -> PredictionKey:
    return (
        item["tenant_id"],
        item["cell_id"],
        item["window_start"],
        item["category"],
    )


class ForecastRepository(Protocol):
    development_only: bool

    def publish(self, tenant_id: str, forecasts: list[dict[str, Any]]) -> None: ...
    def list_window(self, tenant_id: str, window_start: str, category: str) -> list[dict[str, Any]]: ...
    def get(self, tenant_id: str, forecast_id: str) -> dict[str, Any] | None: ...


class InMemoryForecastRepository:
    development_only = True

    def __init__(self) -> None:
        self._by_key: dict[PredictionKey, dict[str, Any]] = {}
        self._by_id: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def publish(self, tenant_id: str, forecasts: list[dict[str, Any]]) -> None:
        prepared: list[dict[str, Any]] = []
        seen: set[PredictionKey] = set()
        for item in forecasts:
            validate_contract("forecast", item)
            if item["tenant_id"] != tenant_id:
                raise DataContractError("Forecast publication tenant mismatch")
            key = _key(item)
            if key in seen:
                raise DataContractError("Forecast publication contains a duplicate prediction key")
            seen.add(key)
            prepared.append(json.loads(json.dumps(item)))
        with self._lock:
            for item in prepared:
                key = _key(item)
                previous = self._by_key.get(key)
                if previous is not None:
                    self._by_id.pop((tenant_id, previous["forecast_id"]), None)
                self._by_key[key] = item
                self._by_id[(tenant_id, item["forecast_id"])] = item

    def list_window(self, tenant_id: str, window_start: str, category: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(item))
                for key, item in sorted(self._by_key.items())
                if key[0] == tenant_id and key[2] == window_start and key[3] == category
            ]

    def get(self, tenant_id: str, forecast_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._by_id.get((tenant_id, forecast_id))
            return json.loads(json.dumps(item)) if item is not None else None


class PostgresForecastRepository:
    """Production repository; TenantPostgres establishes transaction-scoped RLS."""

    development_only = False

    def __init__(self, database: Any) -> None:
        self.database = database

    def publish(self, tenant_id: str, forecasts: list[dict[str, Any]]) -> None:
        prepared: list[dict[str, Any]] = []
        seen: set[PredictionKey] = set()
        for item in forecasts:
            validate_contract("forecast", item)
            if item["tenant_id"] != tenant_id:
                raise DataContractError("Forecast publication tenant mismatch")
            if _key(item) in seen:
                raise DataContractError("Forecast publication contains a duplicate prediction key")
            seen.add(_key(item))
            prepared.append(item)
        with self.database.transaction(tenant_id) as cursor:
            for item in prepared:
                cursor.execute(
                    """INSERT INTO operational_forecasts
                       (tenant_id, forecast_id, cell_id, window_start, category,
                        feature_snapshot_version, model_version, forecast, generated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                       ON CONFLICT (tenant_id, cell_id, window_start, category)
                       DO UPDATE SET forecast_id=excluded.forecast_id,
                         feature_snapshot_version=excluded.feature_snapshot_version,
                         model_version=excluded.model_version, forecast=excluded.forecast,
                         generated_at=excluded.generated_at""",
                    (
                        tenant_id,
                        item["forecast_id"],
                        item["cell_id"],
                        item["window_start"],
                        item["category"],
                        item["feature_snapshot_version"],
                        item["model_version"],
                        json.dumps(item),
                        item["generated_at"],
                    ),
                )

    def list_window(self, tenant_id: str, window_start: str, category: str) -> list[dict[str, Any]]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT forecast FROM operational_forecasts
                   WHERE tenant_id=%s AND window_start=%s AND category=%s ORDER BY cell_id""",
                (tenant_id, window_start, category),
            )
            return [dict(row["forecast"]) for row in cursor.fetchall()]

    def get(self, tenant_id: str, forecast_id: str) -> dict[str, Any] | None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT forecast FROM operational_forecasts
                   WHERE tenant_id=%s AND forecast_id=%s""",
                (tenant_id, forecast_id),
            )
            row = cursor.fetchone()
        return dict(row["forecast"]) if row else None


class ForecastOrchestrator:
    def __init__(self, service: ForecastService, repository: ForecastRepository) -> None:
        self.service = service
        self.repository = repository

    def publish_future_rows(
        self,
        tenant_id: str,
        rows: list[dict[str, Any]],
        *,
        generated_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not rows:
            raise DataContractError("Scheduled inference requires at least one future row")
        generated = generated_at or datetime.now(timezone.utc)
        intervals = {row.get("interval_start") for row in rows}
        if len(intervals) != 1:
            raise DataContractError("A scheduled inference batch must contain one future window")
        keys: set[tuple[str, str, str]] = set()
        output = []
        for row in rows:
            validate_contract("forecast-feature-row", row)
            if row["tenant_id"] != tenant_id:
                raise DataContractError("Scheduled feature tenant mismatch")
            if parse_utc(row["data_as_of"]) >= parse_utc(row["interval_start"]):
                raise DataContractError("Scheduled features are not strictly future-facing")
            row_key = (row["cell_id"], row["interval_start"], row["category"])
            if row_key in keys:
                raise DataContractError("Scheduled feature batch contains a duplicate prediction key")
            keys.add(row_key)
            output.append(self.service.forecast(row, tenant_id=tenant_id, generated_at=generated))
        self.repository.publish(tenant_id, output)
        return output


class ScheduledForecastRunner:
    """External-scheduler entrypoint; one generator is registered per tenant."""

    def __init__(self, orchestrator: ForecastOrchestrator, generators: dict[str, Any]) -> None:
        self.orchestrator = orchestrator
        self.generators = dict(generators)

    def run_once(self, now: datetime | None = None) -> dict[str, int]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        results: dict[str, int] = {}
        for tenant_id, generator in sorted(self.generators.items()):
            rows = generator.run(current)
            published = self.orchestrator.publish_future_rows(
                tenant_id, rows, generated_at=current
            )
            results[tenant_id] = len(published)
        return results
