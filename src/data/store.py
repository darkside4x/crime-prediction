"""Local durable ingestion state for the recorded hackathon demo."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import h3

from .contracts import validate_contract
from .source import SourceDefinition


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class IngestionStore:
    """SQLite-backed restricted store.

    This database remains inside the ingestion boundary because accepted and
    quarantined events may contain raw coordinates.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id)
                );

                CREATE TABLE IF NOT EXISTS accepted_events (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    external_event_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    accuracy_meters REAL,
                    source_sequence_json TEXT,
                    attributes_json TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id, external_event_id),
                    FOREIGN KEY (tenant_id, source_id)
                      REFERENCES sources (tenant_id, source_id)
                );

                CREATE INDEX IF NOT EXISTS accepted_events_tenant_time
                  ON accepted_events (tenant_id, occurred_at);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id),
                    FOREIGN KEY (tenant_id, source_id)
                      REFERENCES sources (tenant_id, source_id)
                );

                CREATE TABLE IF NOT EXISTS quarantine_restricted (
                    quarantine_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    checkpoint_json TEXT,
                    reason_code TEXT NOT NULL,
                    safe_detail TEXT NOT NULL,
                    payload_json TEXT,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS quarantine_tenant_source
                  ON quarantine_restricted (tenant_id, source_id);

                CREATE TABLE IF NOT EXISTS ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checkpoint_json TEXT,
                    accepted_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    last_received_at TEXT,
                    last_event_at TEXT,
                    last_error_code TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                """
            )

    def register_source(self, source: SourceDefinition) -> None:
        definition = source.public_record()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    tenant_id, source_id, schema_version, definition_json, registered_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, source_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    definition_json = excluded.definition_json
                """,
                (
                    source.tenant_id,
                    source.source_id,
                    source.schema_version,
                    json.dumps(definition, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                ),
            )

    def register_camera_source(self, payload: dict[str, Any]) -> None:
        """Register a camera source as an accepted-event foreign-key parent.

        The full camera definition remains in the restricted video store. This
        compatibility record contains secret references, never resolved values.
        """
        validate_contract("camera-source.schema.json", payload)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO sources (
                       tenant_id, source_id, schema_version, definition_json, registered_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, source_id) DO UPDATE SET
                       schema_version=excluded.schema_version,
                       definition_json=excluded.definition_json""",
                (
                    payload["tenant_id"],
                    payload["source_id"],
                    payload["schema_version"],
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                ),
            )

    def start_run(self, source: SourceDefinition) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ingestion_runs (
                    run_id, tenant_id, source_id, mode, status, started_at
                ) VALUES (?, ?, ?, 'recorded_replay', 'running', ?)
                """,
                (run_id, source.tenant_id, source.source_id, utc_now()),
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
        finished_at = utc_now() if status in {"completed", "failed", "cancelled"} else None
        checkpoint_json = json.dumps(checkpoint) if checkpoint is not None else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE ingestion_runs SET
                    status = ?, checkpoint_json = ?, accepted_count = ?,
                    duplicate_count = ?, rejected_count = ?, last_received_at = ?,
                    last_event_at = ?, last_error_code = ?, finished_at = ?
                WHERE run_id = ? AND (? IS NULL OR tenant_id = ?)
                """,
                (
                    status,
                    checkpoint_json,
                    accepted_count,
                    duplicate_count,
                    rejected_count,
                    last_received_at,
                    last_event_at,
                    last_error_code,
                    finished_at,
                    run_id,
                    tenant_id,
                    tenant_id,
                ),
            )
        run = self.get_run(run_id, tenant_id=tenant_id)
        validate_contract("ingestion-run.schema.json", run)
        return run

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_runs WHERE run_id = ? AND (? IS NULL OR tenant_id = ?)",
                (run_id, tenant_id, tenant_id),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {
            "schema_version": "1.0.0",
            "tenant_id": row["tenant_id"],
            "source_id": row["source_id"],
            "run_id": row["run_id"],
            "mode": row["mode"],
            "status": row["status"],
            "checkpoint": json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None,
            "accepted_count": row["accepted_count"],
            "duplicate_count": row["duplicate_count"],
            "rejected_count": row["rejected_count"],
            "last_received_at": row["last_received_at"],
            "last_event_at": row["last_event_at"],
            "last_error_code": row["last_error_code"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def get_checkpoint(self, tenant_id: str, source_id: str) -> str | int | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT checkpoint_json FROM checkpoints
                WHERE tenant_id = ? AND source_id = ?
                """,
                (tenant_id, source_id),
            ).fetchone()
        return json.loads(row["checkpoint_json"]) if row else None

    def set_checkpoint(self, tenant_id: str, source_id: str, checkpoint: str | int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (tenant_id, source_id, checkpoint_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (tenant_id, source_id) DO UPDATE SET
                    checkpoint_json = excluded.checkpoint_json,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, source_id, json.dumps(checkpoint), utc_now()),
            )

    def insert_event(self, event: dict[str, Any], event_hash: str) -> bool:
        """Insert an event, returning False when the idempotency key exists."""
        location = event["location"]
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO accepted_events (
                        tenant_id, source_id, external_event_id, occurred_at,
                        received_at, category, latitude, longitude, accuracy_meters,
                        source_sequence_json, attributes_json, event_hash, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["tenant_id"],
                        event["source_id"],
                        event["external_event_id"],
                        event["occurred_at"],
                        event["received_at"],
                        event["category"],
                        location["latitude"],
                        location["longitude"],
                        location.get("accuracy_meters"),
                        json.dumps(event.get("source_sequence")),
                        json.dumps(event.get("attributes", {}), sort_keys=True),
                        event_hash,
                        utc_now(),
                    ),
                )
            return True
        except sqlite3.IntegrityError as error:
            if "UNIQUE constraint failed" in str(error):
                with self.connect() as connection:
                    existing = connection.execute(
                        """
                        SELECT event_hash FROM accepted_events
                        WHERE tenant_id = ? AND source_id = ? AND external_event_id = ?
                        """,
                        (event["tenant_id"], event["source_id"], event["external_event_id"]),
                    ).fetchone()
                if existing and existing["event_hash"] == event_hash:
                    return False
                from .errors import IngestionError

                raise IngestionError(
                    "idempotency_conflict",
                    "The event id already exists with different normalized content",
                ) from error
            raise

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
        payload_json = json.dumps(payload, sort_keys=True) if payload is not None else None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO quarantine_restricted (
                    quarantine_id, tenant_id, source_id, checkpoint_json,
                    reason_code, safe_detail, payload_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    tenant_id,
                    source_id,
                    json.dumps(checkpoint),
                    reason_code,
                    safe_detail[:500],
                    payload_json,
                    payload_hash,
                    utc_now(),
                ),
            )

    def list_events(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM accepted_events
                WHERE tenant_id = ?
                ORDER BY occurred_at, source_id, external_event_id
                """,
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_aggregated_events(
        self,
        tenant_id: str,
        h3_resolution: int,
        source_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Cross the privacy boundary by replacing coordinates with H3 cells.

        The returned records intentionally omit source event identity and raw
        coordinates, so feature code cannot access either field.
        """
        if not source_ids:
            raise ValueError("At least one source_id is required")
        placeholders = ",".join("?" for _ in source_ids)
        # Only placeholder count is interpolated; every source ID remains a bound value.
        query = (
            "SELECT occurred_at, received_at, category, latitude, longitude "
            "FROM accepted_events "
            f"WHERE tenant_id = ? AND source_id IN ({placeholders}) "  # nosec B608
            "ORDER BY occurred_at"
        )
        with self.connect() as connection:
            rows = connection.execute(
                query,
                (tenant_id, *source_ids),
            ).fetchall()
        return [
            {
                "occurred_at": row["occurred_at"],
                "received_at": row["received_at"],
                "category": row["category"],
                "cell_id": h3.latlng_to_cell(row["latitude"], row["longitude"], h3_resolution),
            }
            for row in rows
        ]

    def event_count(self, tenant_id: str, source_ids: tuple[str, ...] | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM accepted_events WHERE tenant_id = ?"
        parameters: list[Any] = [tenant_id]
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            query += f" AND source_id IN ({placeholders})"
            parameters.extend(source_ids)
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return int(row["count"])

    def quarantine_count(self, tenant_id: str, source_id: str | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM quarantine_restricted WHERE tenant_id = ?"
        parameters: list[Any] = [tenant_id]
        if source_id is not None:
            query += " AND source_id = ?"
            parameters.append(source_id)
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return int(row["count"])
