"""Tenant-aware, idempotent ingestion orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .adapters.base import EventSourceAdapter
from .category_map import CategoryMap
from .contracts import validate_contract
from .errors import ContractValidationError, IngestionError
from .source import SourceDefinition
from .store import IngestionStore


ALLOWED_ATTRIBUTE_KEYS = frozenset({"reporting_channel", "source_quality", "coverage_status"})
MAX_SOURCE_CLOCK_SKEW = timedelta(minutes=5)


def _payload_hash(payload: object) -> str:
    if isinstance(payload, str):
        encoded = payload.encode("utf-8", errors="replace")
    else:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_utc(value: object, field_name: str) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise IngestionError("timestamp_invalid", f"{field_name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IngestionError("timestamp_invalid", f"{field_name} is not a valid ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IngestionError("timestamp_timezone_missing", f"{field_name} must include a timezone")
    utc_value = parsed.astimezone(timezone.utc)
    return utc_value.isoformat().replace("+00:00", "Z"), utc_value


class IngestionService:
    def __init__(self, store: IngestionStore, category_map: CategoryMap, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.category_map = category_map
        self.max_attempts = max_attempts

    def normalize_event(
        self,
        payload: dict[str, Any],
        *,
        authenticated_tenant_id: str,
        source: SourceDefinition,
    ) -> dict[str, Any]:
        if payload.get("tenant_id") != authenticated_tenant_id:
            raise IngestionError("event_tenant_mismatch", "Event tenant does not match authenticated context")
        if payload.get("source_id") != source.source_id:
            raise IngestionError("event_source_mismatch", "Event source does not match its adapter")

        normalized = dict(payload)
        normalized["category"] = self.category_map.normalize(payload.get("category"))
        normalized["occurred_at"], occurred_at = _parse_utc(payload.get("occurred_at"), "occurred_at")
        normalized["received_at"], received_at = _parse_utc(payload.get("received_at"), "received_at")
        if received_at + MAX_SOURCE_CLOCK_SKEW < occurred_at:
            raise IngestionError(
                "received_before_occurred",
                "received_at is earlier than occurred_at beyond the clock-skew allowance",
            )

        attributes = payload.get("attributes", {})
        if not isinstance(attributes, dict):
            raise IngestionError("attributes_invalid", "attributes must be an object")
        normalized["attributes"] = {
            key: value for key, value in attributes.items() if key in ALLOWED_ATTRIBUTE_KEYS
        }
        validate_contract("incident-event.schema.json", normalized)
        return normalized

    async def _insert_with_retry(self, event: dict[str, Any], event_hash: str) -> bool:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.store.insert_event(event, event_hash)
            except sqlite3.OperationalError as error:
                if attempt == self.max_attempts:
                    raise IngestionError(
                        "restricted_store_unavailable",
                        "Restricted ingestion store remained unavailable after bounded retries",
                        retryable=True,
                    ) from error
                await asyncio.sleep(0.05 * (2 ** (attempt - 1)))
        raise AssertionError("unreachable")

    async def ingest_replay(
        self,
        adapter: EventSourceAdapter,
        source: SourceDefinition,
        *,
        authenticated_tenant_id: str,
    ) -> dict[str, Any]:
        source.authorize(authenticated_tenant_id)
        health = await adapter.validate_connection()
        if not health.healthy:
            raise IngestionError("source_validation_failed", health.detail)

        self.store.register_source(source)
        run_id = self.store.start_run(source)
        checkpoint = self.store.get_checkpoint(source.tenant_id, source.source_id)
        accepted_count = 0
        duplicate_count = 0
        rejected_count = 0
        last_received_at: str | None = None
        last_event_at: str | None = None
        last_error_code: str | None = None

        try:
            async for item in adapter.read(checkpoint):
                checkpoint = item.checkpoint
                if item.error_code:
                    rejected_count += 1
                    last_error_code = item.error_code
                    raw_value = item.raw_value or ""
                    self.store.quarantine(
                        tenant_id=source.tenant_id,
                        source_id=source.source_id,
                        checkpoint=checkpoint,
                        reason_code=item.error_code,
                        safe_detail=item.safe_detail or "Adapter rejected a record",
                        payload=raw_value,
                        payload_hash=_payload_hash(raw_value),
                    )
                    await adapter.commit(checkpoint)
                    continue

                try:
                    if item.payload is None:
                        raise IngestionError(
                            "adapter_payload_missing",
                            "Adapter item has neither a payload nor an error code",
                        )
                    event = self.normalize_event(
                        item.payload,
                        authenticated_tenant_id=authenticated_tenant_id,
                        source=source,
                    )
                    event_hash = _payload_hash(event)
                    inserted = await self._insert_with_retry(event, event_hash)
                    if inserted:
                        accepted_count += 1
                        last_received_at = event["received_at"]
                        last_event_at = event["occurred_at"]
                    else:
                        duplicate_count += 1
                except (ContractValidationError, IngestionError) as error:
                    rejected_count += 1
                    last_error_code = error.code
                    self.store.quarantine(
                        tenant_id=source.tenant_id,
                        source_id=source.source_id,
                        checkpoint=checkpoint,
                        reason_code=error.code,
                        safe_detail=str(error),
                        payload=item.payload,
                        payload_hash=_payload_hash(item.payload),
                    )
                await adapter.commit(checkpoint)

            return self.store.finish_run(
                run_id,
                status="completed",
                checkpoint=checkpoint,
                accepted_count=accepted_count,
                duplicate_count=duplicate_count,
                rejected_count=rejected_count,
                last_received_at=last_received_at,
                last_event_at=last_event_at,
                last_error_code=last_error_code,
            )
        except Exception as error:
            error_code = error.code if isinstance(error, IngestionError) else "unexpected_ingestion_failure"
            self.store.finish_run(
                run_id,
                status="failed",
                checkpoint=checkpoint,
                accepted_count=accepted_count,
                duplicate_count=duplicate_count,
                rejected_count=rejected_count,
                last_received_at=last_received_at,
                last_event_at=last_event_at,
                last_error_code=error_code,
            )
            raise
