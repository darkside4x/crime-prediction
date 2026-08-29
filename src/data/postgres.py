"""PostgreSQL ingestion repository with transaction-scoped tenant RLS.

The application role must not have ``BYPASSRLS``. Every repository operation
opens a transaction and executes ``SET LOCAL app.tenant_id`` before touching a
tenant table. Raw coordinates remain confined to this module.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import h3

from .contracts import validate_contract
from .errors import IngestionError
from .source import SourceDefinition
from .store import utc_now


class TenantPostgres:
    """Small connection-pool wrapper that makes tenant context unavoidable."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL DSN")
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as error:  # pragma: no cover - dependency guidance
            raise RuntimeError(
                "Install the platform extra: pip install -e '.[platform]'"
            ) from error
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True
        )

    @contextmanager
    def transaction(self, tenant_id: str) -> Iterator[Any]:
        try:
            uuid.UUID(tenant_id)
        except (TypeError, ValueError) as error:
            raise ValueError("tenant_id must be a UUID") from error
        from psycopg import sql
        from psycopg.rows import dict_row

        with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(tenant_id))
            )
            yield cursor

    @contextmanager
    def system_transaction(self) -> Iterator[Any]:
        """Access non-tenant operational tables only.

        Tenant repositories must use :meth:`transaction`; this deliberately
        does not set ``app.tenant_id`` and therefore cannot read RLS-protected
        tenant rows.
        """
        from psycopg.rows import dict_row

        with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            yield cursor

    def close(self) -> None:
        self._pool.close()


class PostgresIngestionStore:
    """Production replacement for the SQLite ``IngestionStore``."""

    def __init__(self, database: TenantPostgres) -> None:
        self.database = database

    def register_source(self, source: SourceDefinition) -> None:
        definition = source.public_record()
        with self.database.transaction(source.tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO incident_sources_restricted
                   (tenant_id, source_id, definition, registered_at)
                   VALUES (%s, %s, %s::jsonb, %s)
                   ON CONFLICT (tenant_id, source_id) DO UPDATE
                   SET definition=excluded.definition""",
                (source.tenant_id, source.source_id, json.dumps(definition), utc_now()),
            )

    def register_camera_source(self, payload: dict[str, Any]) -> None:
        validate_contract("camera-source.schema.json", payload)
        with self.database.transaction(payload["tenant_id"]) as cursor:
            cursor.execute(
                """INSERT INTO incident_sources_restricted
                   (tenant_id, source_id, definition, registered_at)
                   VALUES (%s, %s, %s::jsonb, %s)
                   ON CONFLICT (tenant_id, source_id) DO UPDATE
                   SET definition=excluded.definition""",
                (
                    payload["tenant_id"],
                    payload["source_id"],
                    json.dumps(payload),
                    utc_now(),
                ),
            )

    def start_run(self, source: SourceDefinition) -> str:
        run_id = str(uuid.uuid4())
        with self.database.transaction(source.tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO ingestion_runs
                   (tenant_id, run_id, source_id, mode, status, started_at)
                   VALUES (%s, %s, %s, 'recorded_replay', 'running', %s)""",
                (source.tenant_id, run_id, source.source_id, utc_now()),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        status: str,
        checkpoint: str | int | None,
        accepted_count: int,
        duplicate_count: int,
        rejected_count: int,
        last_received_at: str | None,
        last_event_at: str | None,
        last_error_code: str | None = None,
    ) -> dict[str, Any]:
        if tenant_id is None:
            raise ValueError("tenant_id is required by the PostgreSQL repository")
        finished_at = (
            utc_now() if status in {"completed", "failed", "cancelled"} else None
        )
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE ingestion_runs SET status=%s, checkpoint=%s::jsonb,
                   accepted_count=%s, duplicate_count=%s, rejected_count=%s,
                   last_received_at=%s, last_event_at=%s, last_error_code=%s,
                   finished_at=%s WHERE tenant_id=%s AND run_id=%s""",
                (
                    status,
                    json.dumps(checkpoint) if checkpoint is not None else None,
                    accepted_count,
                    duplicate_count,
                    rejected_count,
                    last_received_at,
                    last_event_at,
                    last_error_code,
                    finished_at,
                    tenant_id,
                    run_id,
                ),
            )
        result = self.get_run(run_id, tenant_id=tenant_id)
        validate_contract("ingestion-run.schema.json", result)
        return result

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        if tenant_id is None:
            raise ValueError("tenant_id is required by the PostgreSQL repository")
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                "SELECT * FROM ingestion_runs WHERE tenant_id=%s AND run_id=%s",
                (tenant_id, run_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        return {
            "schema_version": "1.0.0",
            "tenant_id": str(row["tenant_id"]),
            "source_id": str(row["source_id"]),
            "run_id": str(row["run_id"]),
            "mode": row["mode"],
            "status": row["status"],
            "checkpoint": row["checkpoint"],
            "accepted_count": row["accepted_count"],
            "duplicate_count": row["duplicate_count"],
            "rejected_count": row["rejected_count"],
            "last_received_at": _time(row["last_received_at"]),
            "last_event_at": _time(row["last_event_at"]),
            "last_error_code": row["last_error_code"],
            "started_at": _time(row["started_at"]),
            "finished_at": _time(row["finished_at"]),
        }

    def get_checkpoint(self, tenant_id: str, source_id: str) -> str | int | None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                "SELECT checkpoint FROM ingestion_checkpoints WHERE tenant_id=%s AND source_id=%s",
                (tenant_id, source_id),
            )
            row = cursor.fetchone()
        return row["checkpoint"] if row else None

    def set_checkpoint(
        self, tenant_id: str, source_id: str, checkpoint: str | int
    ) -> None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO ingestion_checkpoints
                   (tenant_id, source_id, checkpoint, updated_at)
                   VALUES (%s, %s, %s::jsonb, %s)
                   ON CONFLICT (tenant_id, source_id) DO UPDATE
                   SET checkpoint=excluded.checkpoint, updated_at=excluded.updated_at""",
                (tenant_id, source_id, json.dumps(checkpoint), utc_now()),
            )

    def insert_event(self, event: dict[str, Any], event_hash: str) -> bool:
        tenant_id = event["tenant_id"]
        location = event["location"]
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO accepted_incident_events_restricted
                   (tenant_id, source_id, external_event_id, event, event_hash,
                    occurred_at, received_at, category, latitude, longitude, ingested_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, source_id, external_event_id) DO NOTHING""",
                (
                    tenant_id,
                    event["source_id"],
                    event["external_event_id"],
                    json.dumps(event),
                    event_hash,
                    event["occurred_at"],
                    event["received_at"],
                    event["category"],
                    location["latitude"],
                    location["longitude"],
                    utc_now(),
                ),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                cursor.execute(
                    """SELECT event_hash FROM accepted_incident_events_restricted
                       WHERE tenant_id=%s AND source_id=%s AND external_event_id=%s""",
                    (tenant_id, event["source_id"], event["external_event_id"]),
                )
                row = cursor.fetchone()
                if row and row["event_hash"] != event_hash:
                    raise IngestionError(
                        "idempotency_conflict",
                        "The event id already exists with different normalized content",
                    )
        return inserted

    def quarantine(
        self,
        *,
        tenant_id: str,
        source_id: str,
        checkpoint: str | int | None,
        reason_code: str,
        safe_detail: str,
        payload: dict[str, Any] | str | None,
        payload_hash: str,
    ) -> None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO ingestion_quarantine_restricted
                   (tenant_id, quarantine_id, source_id, checkpoint, reason_code,
                    safe_detail, payload, payload_hash, created_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s)""",
                (
                    tenant_id,
                    str(uuid.uuid4()),
                    source_id,
                    json.dumps(checkpoint),
                    reason_code,
                    safe_detail[:500],
                    json.dumps(payload) if payload is not None else None,
                    payload_hash,
                    utc_now(),
                ),
            )

    def list_events(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT event FROM accepted_incident_events_restricted
                   WHERE tenant_id=%s ORDER BY occurred_at, source_id, external_event_id""",
                (tenant_id,),
            )
            rows = cursor.fetchall()
        return [dict(row["event"]) for row in rows]

    def list_aggregated_events(
        self, tenant_id: str, h3_resolution: int, source_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        if not source_ids:
            raise ValueError("At least one source_id is required")
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT occurred_at, received_at, category, latitude, longitude
                   FROM accepted_incident_events_restricted
                   WHERE tenant_id=%s AND source_id = ANY(%s::uuid[])
                   ORDER BY occurred_at""",
                (tenant_id, list(source_ids)),
            )
            rows = cursor.fetchall()
        return [
            {
                "occurred_at": _time(row["occurred_at"]),
                "received_at": _time(row["received_at"]),
                "category": row["category"],
                "cell_id": h3.latlng_to_cell(
                    row["latitude"], row["longitude"], h3_resolution
                ),
            }
            for row in rows
        ]

    def event_count(
        self, tenant_id: str, source_ids: tuple[str, ...] | None = None
    ) -> int:
        with self.database.transaction(tenant_id) as cursor:
            if source_ids:
                cursor.execute(
                    """SELECT count(*) AS count FROM accepted_incident_events_restricted
                       WHERE tenant_id=%s AND source_id = ANY(%s::uuid[])""",
                    (tenant_id, list(source_ids)),
                )
            else:
                cursor.execute(
                    "SELECT count(*) AS count FROM accepted_incident_events_restricted WHERE tenant_id=%s",
                    (tenant_id,),
                )
            row = cursor.fetchone()
        return int(row["count"])

    def quarantine_count(self, tenant_id: str, source_id: str | None = None) -> int:
        with self.database.transaction(tenant_id) as cursor:
            if source_id:
                cursor.execute(
                    """SELECT count(*) AS count FROM ingestion_quarantine_restricted
                       WHERE tenant_id=%s AND source_id=%s""",
                    (tenant_id, source_id),
                )
            else:
                cursor.execute(
                    "SELECT count(*) AS count FROM ingestion_quarantine_restricted WHERE tenant_id=%s",
                    (tenant_id,),
                )
            row = cursor.fetchone()
        return int(row["count"])


def _time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)
