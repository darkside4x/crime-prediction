"""Tenant-scoped durable state for recorded-video processing."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.data.store import IngestionStore, utc_now

from .errors import VideoPipelineError


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class VideoStore:
    """Restricted video tables co-located with the ingestion-boundary DB."""

    def __init__(self, ingestion_store: IngestionStore) -> None:
        self.ingestion_store = ingestion_store
        self._initialize()

    def _initialize(self) -> None:
        with self.ingestion_store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS camera_sources_restricted (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS video_assets_restricted (
                    tenant_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, asset_id),
                    FOREIGN KEY (tenant_id, source_id)
                      REFERENCES camera_sources_restricted (tenant_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS reka_video_registry_restricted (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    reka_video_id TEXT NOT NULL,
                    indexing_status TEXT NOT NULL,
                    remote_deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, asset_id),
                    UNIQUE (tenant_id, reka_video_id)
                );
                CREATE TABLE IF NOT EXISTS video_processing_jobs (
                    tenant_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    last_error_code TEXT,
                    safe_diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    available_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    worker_id TEXT,
                    dead_lettered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, job_id),
                    UNIQUE (tenant_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS video_jobs_ready
                  ON video_processing_jobs (state, available_at);
                CREATE UNIQUE INDEX IF NOT EXISTS video_jobs_one_active_analysis
                  ON video_processing_jobs (tenant_id, asset_id)
                  WHERE operation='analyze' AND state IN ('queued','running','retry');
                CREATE TABLE IF NOT EXISTS candidate_detections_restricted (
                    tenant_id TEXT NOT NULL,
                    detection_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    semantic_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, detection_id),
                    UNIQUE (tenant_id, semantic_key)
                );
                CREATE TABLE IF NOT EXISTS candidate_reviews_restricted (
                    tenant_id TEXT NOT NULL,
                    review_id TEXT NOT NULL,
                    detection_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, review_id),
                    UNIQUE (tenant_id, detection_id)
                );
                CREATE TABLE IF NOT EXISTS coverage_snapshots (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    interval_start TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id, interval_start)
                );
                CREATE TABLE IF NOT EXISTS future_feature_snapshots (
                    tenant_id TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    interval_start TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, snapshot_version)
                );
                CREATE TABLE IF NOT EXISTS video_audit_log (
                    tenant_id TEXT NOT NULL,
                    audit_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, audit_id)
                );
                """
            )

            # Existing development databases predate durable worker leases.
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(video_processing_jobs)").fetchall()
            }
            for name in ("lease_expires_at", "heartbeat_at", "worker_id", "dead_lettered_at"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE video_processing_jobs ADD COLUMN {name} TEXT")
            if "safe_diagnostics_json" not in columns:
                connection.execute(
                    "ALTER TABLE video_processing_jobs "
                    "ADD COLUMN safe_diagnostics_json TEXT NOT NULL DEFAULT '{}'"
                )

    def put_source(self, payload: dict[str, Any]) -> None:
        with self.ingestion_store.connect() as connection:
            connection.execute(
                "INSERT INTO camera_sources_restricted VALUES (?, ?, ?, ?) "
                "ON CONFLICT (tenant_id, source_id) DO UPDATE SET payload_json=excluded.payload_json",
                (payload["tenant_id"], payload["source_id"], _json(payload), payload["created_at"]),
            )

    def get_source(self, tenant_id: str, source_id: str) -> dict[str, Any]:
        return self._get_payload("camera_sources_restricted", tenant_id, "source_id", source_id)

    def list_source_ids(self, tenant_id: str) -> tuple[str, ...]:
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                """SELECT source_id, payload_json FROM camera_sources_restricted
                   WHERE tenant_id=? ORDER BY source_id""",
                (tenant_id,),
            ).fetchall()
        return tuple(
            row["source_id"]
            for row in rows
            if json.loads(row["payload_json"]).get("status") == "active"
        )

    def put_asset(self, payload: dict[str, Any], local_path: Path | str) -> None:
        stored_path = str(local_path)
        stored_path = stored_path.removeprefix("file://")
        with self.ingestion_store.connect() as connection:
            connection.execute(
                """INSERT INTO video_assets_restricted
                   (tenant_id, asset_id, source_id, payload_json, local_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, asset_id) DO UPDATE SET payload_json=excluded.payload_json""",
                (payload["tenant_id"], payload["asset_id"], payload["source_id"], _json(payload), stored_path, utc_now()),
            )

    def get_asset(self, tenant_id: str, asset_id: str) -> dict[str, Any]:
        return self._get_payload("video_assets_restricted", tenant_id, "asset_id", asset_id)

    def asset_path(self, tenant_id: str, asset_id: str) -> Path:
        with self.ingestion_store.connect() as connection:
            row = connection.execute(
                "SELECT local_path FROM video_assets_restricted WHERE tenant_id=? AND asset_id=?",
                (tenant_id, asset_id),
            ).fetchone()
        if row is None:
            raise VideoPipelineError("asset_not_found", "Video asset was not found")
        return Path(row["local_path"])

    def asset_storage_ref(self, tenant_id: str, asset_id: str) -> str:
        return f"file://{self.asset_path(tenant_id, asset_id).resolve()}"

    def update_asset_status(self, tenant_id: str, asset_id: str, status: str, failure_code: str | None = None) -> None:
        payload = self.get_asset(tenant_id, asset_id)
        payload["status"] = status
        if failure_code:
            payload["failure_code"] = failure_code
        else:
            payload.pop("failure_code", None)
        self.put_asset(payload, self.asset_path(tenant_id, asset_id))

    def tenant_asset_bytes(self, tenant_id: str) -> int:
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM video_assets_restricted WHERE tenant_id=?", (tenant_id,)
            ).fetchall()
        return sum(
            int(value["size_bytes"])
            for row in rows
            if (value := json.loads(row["payload_json"]))["status"] != "deleted"
        )

    def put_mapping(self, tenant_id: str, source_id: str, asset_id: str, reka_video_id: str, status: str) -> None:
        now = utc_now()
        with self.ingestion_store.connect() as connection:
            connection.execute(
                """INSERT INTO reka_video_registry_restricted
                   (tenant_id, source_id, asset_id, reka_video_id, indexing_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, asset_id) DO UPDATE SET
                     indexing_status=excluded.indexing_status, updated_at=excluded.updated_at""",
                (tenant_id, source_id, asset_id, reka_video_id, status, now, now),
            )

    def get_mapping(self, tenant_id: str, asset_id: str) -> dict[str, Any] | None:
        with self.ingestion_store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reka_video_registry_restricted WHERE tenant_id=? AND asset_id=?",
                (tenant_id, asset_id),
            ).fetchone()
        return dict(row) if row else None

    def mapping_by_remote_id(self, tenant_id: str, reka_video_id: str) -> dict[str, Any] | None:
        with self.ingestion_store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reka_video_registry_restricted WHERE tenant_id=? AND reka_video_id=?",
                (tenant_id, reka_video_id),
            ).fetchone()
        return dict(row) if row else None

    def mark_remote_deleted(self, tenant_id: str, asset_id: str) -> None:
        now = utc_now()
        with self.ingestion_store.connect() as connection:
            connection.execute(
                "UPDATE reka_video_registry_restricted SET remote_deleted_at=?, updated_at=? WHERE tenant_id=? AND asset_id=?",
                (now, now, tenant_id, asset_id),
            )

    def enqueue(
        self,
        tenant_id: str,
        asset_id: str,
        operation: str,
        *,
        max_attempts: int = 5,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or f"{asset_id}:{operation}"
        now = utc_now()
        with self.ingestion_store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM video_processing_jobs WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, key),
            ).fetchone()
            if row:
                return self._job(row)
            job_id = str(uuid.uuid4())
            try:
                connection.execute(
                    """INSERT INTO video_processing_jobs
                       (tenant_id, job_id, asset_id, operation, idempotency_key, state, attempts,
                        max_attempts, available_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)""",
                    (tenant_id, job_id, asset_id, operation, key, max_attempts, now, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise VideoPipelineError(
                    "job_active_conflict", "Equivalent processing work is already active"
                ) from error
        return self.get_job(tenant_id, job_id)

    def get_job(self, tenant_id: str, job_id: str) -> dict[str, Any]:
        with self.ingestion_store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM video_processing_jobs WHERE tenant_id=? AND job_id=?", (tenant_id, job_id)
            ).fetchone()
        if row is None:
            raise VideoPipelineError("job_not_found", "Processing job was not found")
        return self._job(row)

    def list_jobs(self, tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM video_processing_jobs WHERE tenant_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [self._job(row) for row in rows]

    def transition_job(
        self,
        tenant_id: str,
        job_id: str,
        state: str,
        error_code: str | None = None,
        *,
        retry_delay_seconds: float = 0,
        safe_diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in {"queued", "running", "completed", "failed", "cancelled", "retry"}:
            raise ValueError("Invalid job state")
        now = utc_now()
        with self.ingestion_store.connect() as connection:
            current = connection.execute(
                "SELECT * FROM video_processing_jobs WHERE tenant_id=? AND job_id=?", (tenant_id, job_id)
            ).fetchone()
            if current is None:
                raise VideoPipelineError("job_not_found", "Processing job was not found")
            attempts = current["attempts"] + (1 if state == "running" else 0)
            started = now if state == "running" else current["started_at"]
            finished = now if state in {"completed", "failed", "cancelled"} else None
            available_at = (
                datetime.now(UTC) + timedelta(seconds=max(retry_delay_seconds, 0))
            ).isoformat().replace("+00:00", "Z")
            connection.execute(
                """UPDATE video_processing_jobs SET state=?, attempts=?, last_error_code=?,
                   safe_diagnostics_json=?,
                   started_at=?, finished_at=?, available_at=?, lease_expires_at=NULL,
                   heartbeat_at=NULL, worker_id=NULL, updated_at=?
                   WHERE tenant_id=? AND job_id=?""",
                (
                    state, attempts, error_code, _json(safe_diagnostics or {}),
                    started, finished, available_at,
                    now, tenant_id, job_id,
                ),
            )
        return self.get_job(tenant_id, job_id)

    def ready_jobs(self, *, operations: tuple[str, ...], limit: int = 10) -> list[dict[str, Any]]:
        if not operations or limit < 1:
            return []
        placeholders = ",".join("?" for _ in operations)
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM video_processing_jobs
                    WHERE state IN ('queued','retry') AND available_at <= ?
                      AND operation IN ({placeholders})
                      AND (
                        attempts < max_attempts
                        OR (
                          operation='index' AND state='retry'
                          AND last_error_code='reka_index_pending'
                        )
                      )
                    ORDER BY available_at, created_at LIMIT ?""",  # nosec B608
                (utc_now(), *operations, limit),
            ).fetchall()
        return [self._job(row) for row in rows]

    def claim_job(
        self, tenant_id: str, job_id: str, *, worker_id: str, lease_seconds: int
    ) -> dict[str, Any]:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        lease = (now + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        with self.ingestion_store.connect() as connection:
            cursor = connection.execute(
                """UPDATE video_processing_jobs SET state='running', attempts=attempts+1,
                   started_at=COALESCE(started_at, ?), heartbeat_at=?, lease_expires_at=?,
                   worker_id=?, updated_at=?
                   WHERE tenant_id=? AND job_id=? AND state IN ('queued','retry')
                     AND available_at <= ? AND attempts < max_attempts""",
                (
                    now_text, now_text, lease, worker_id, now_text,
                    tenant_id, job_id, now_text,
                ),
            )
        if cursor.rowcount != 1:
            raise VideoPipelineError("job_not_claimable", "Processing job is not ready")
        return self.get_job(tenant_id, job_id)

    def heartbeat(
        self, tenant_id: str, job_id: str, *, worker_id: str, lease_seconds: int
    ) -> None:
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        lease = (now + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        with self.ingestion_store.connect() as connection:
            cursor = connection.execute(
                """UPDATE video_processing_jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                   WHERE tenant_id=? AND job_id=? AND state='running' AND worker_id=?""",
                (now_text, lease, now_text, tenant_id, job_id, worker_id),
            )
        if cursor.rowcount != 1:
            raise VideoPipelineError("job_lease_lost", "Worker no longer owns the job lease")

    def extend_index_attempt_limit(
        self, tenant_id: str, job_id: str, *, max_attempts: int
    ) -> dict[str, Any]:
        with self.ingestion_store.connect() as connection:
            cursor = connection.execute(
                """UPDATE video_processing_jobs SET max_attempts=?, updated_at=?
                   WHERE tenant_id=? AND job_id=? AND operation='index'
                     AND state='retry' AND last_error_code='reka_index_pending'
                     AND attempts >= max_attempts AND max_attempts < ?""",
                (max_attempts, utc_now(), tenant_id, job_id, max_attempts),
            )
        if cursor.rowcount != 1:
            raise VideoPipelineError(
                "job_attempt_limit_not_extendable",
                "Only an exhausted pending index job can receive the configured bound",
            )
        return self.get_job(tenant_id, job_id)

    def mark_dead_lettered(self, tenant_id: str, job_id: str) -> None:
        now = utc_now()
        with self.ingestion_store.connect() as connection:
            cursor = connection.execute(
                """UPDATE video_processing_jobs SET dead_lettered_at=?, updated_at=?
                   WHERE tenant_id=? AND job_id=? AND state='failed'""",
                (now, now, tenant_id, job_id),
            )
        if cursor.rowcount != 1:
            raise VideoPipelineError("job_not_failed", "Only failed jobs can enter the dead-letter queue")

    def recover_stale_jobs(self, *, stale_after: timedelta, now: datetime | None = None) -> int:
        """Return abandoned running work to retry after a worker lease timeout."""
        if stale_after.total_seconds() <= 0:
            raise ValueError("stale_after must be positive")
        current = now or datetime.now(UTC)
        cutoff = current - stale_after
        current_text = current.astimezone(UTC).isoformat().replace("+00:00", "Z")
        cutoff_text = cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")
        with self.ingestion_store.connect() as connection:
            cursor = connection.execute(
                """UPDATE video_processing_jobs SET state='retry', last_error_code='worker_lease_expired',
                   lease_expires_at=NULL, heartbeat_at=NULL, worker_id=NULL, available_at=?,
                   updated_at=? WHERE state='running'
                   AND ((lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                     OR (lease_expires_at IS NULL AND started_at < ?))
                   AND attempts < max_attempts""",
                (current_text, current_text, current_text, cutoff_text),
            )
            failed = connection.execute(
                """UPDATE video_processing_jobs SET state='failed', last_error_code='worker_recovery_exhausted',
                   finished_at=?, lease_expires_at=NULL, heartbeat_at=NULL, worker_id=NULL,
                   updated_at=? WHERE state='running'
                   AND ((lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                     OR (lease_expires_at IS NULL AND started_at < ?))
                   AND attempts >= max_attempts""",
                (current_text, current_text, current_text, cutoff_text),
            )
        return cursor.rowcount + failed.rowcount

    def put_candidate(self, payload: dict[str, Any], semantic_key: str) -> bool:
        try:
            with self.ingestion_store.connect() as connection:
                connection.execute(
                    """INSERT INTO candidate_detections_restricted
                       (tenant_id, detection_id, source_id, asset_id, semantic_key, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (payload["tenant_id"], payload["detection_id"], payload["source_id"], payload["asset_id"], semantic_key, _json(payload), utc_now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_candidate(self, tenant_id: str, detection_id: str) -> dict[str, Any]:
        return self._get_payload("candidate_detections_restricted", tenant_id, "detection_id", detection_id)

    def list_candidates(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM candidate_detections_restricted WHERE tenant_id=? ORDER BY created_at", (tenant_id,)
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def delete_pending_candidates(self, tenant_id: str) -> int:
        """Delete unreviewed demo-session candidates while preserving final reviews."""
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                """SELECT detection_id, payload_json FROM candidate_detections_restricted
                   WHERE tenant_id=?""",
                (tenant_id,),
            ).fetchall()
            pending_ids = [
                row["detection_id"]
                for row in rows
                if json.loads(row["payload_json"]).get("review_status")
                == "awaiting_review"
            ]
            deleted = 0
            for detection_id in pending_ids:
                cursor = connection.execute(
                    """DELETE FROM candidate_detections_restricted
                       WHERE tenant_id=? AND detection_id=?
                         AND NOT EXISTS (
                           SELECT 1 FROM candidate_reviews_restricted
                           WHERE tenant_id=? AND detection_id=?
                         )""",
                    (tenant_id, detection_id, tenant_id, detection_id),
                )
                deleted += cursor.rowcount
        return deleted

    def update_candidate(self, payload: dict[str, Any]) -> None:
        with self.ingestion_store.connect() as connection:
            cursor = connection.execute(
                "UPDATE candidate_detections_restricted SET payload_json=? WHERE tenant_id=? AND detection_id=?",
                (_json(payload), payload["tenant_id"], payload["detection_id"]),
            )
        if cursor.rowcount != 1:
            raise VideoPipelineError("resource_not_found", "Tenant-scoped candidate was not found")

    def put_review(self, payload: dict[str, Any]) -> None:
        try:
            with self.ingestion_store.connect() as connection:
                connection.execute(
                    """INSERT INTO candidate_reviews_restricted
                       (tenant_id, review_id, detection_id, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (payload["tenant_id"], payload["review_id"], payload["detection_id"], _json(payload), utc_now()),
                )
                candidate = json.loads(connection.execute(
                    "SELECT payload_json FROM candidate_detections_restricted WHERE tenant_id=? AND detection_id=?",
                    (payload["tenant_id"], payload["detection_id"]),
                ).fetchone()["payload_json"])
                candidate["review_status"] = payload["decision"]
                connection.execute(
                    "UPDATE candidate_detections_restricted SET payload_json=? WHERE tenant_id=? AND detection_id=?",
                    (_json(candidate), payload["tenant_id"], payload["detection_id"]),
                )
        except sqlite3.IntegrityError as error:
            raise VideoPipelineError("review_already_final", "Candidate already has an immutable final review") from error

    def put_review_and_event(
        self, payload: dict[str, Any], event: dict[str, Any], event_hash: str
    ) -> None:
        """Atomically finalize a confirmed review and promote exactly one event."""
        location = event["location"]
        try:
            with self.ingestion_store.connect() as connection:
                connection.execute(
                    """INSERT INTO candidate_reviews_restricted
                       (tenant_id, review_id, detection_id, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        payload["tenant_id"], payload["review_id"], payload["detection_id"],
                        _json(payload), utc_now(),
                    ),
                )
                candidate_row = connection.execute(
                    """SELECT payload_json FROM candidate_detections_restricted
                       WHERE tenant_id=? AND detection_id=?""",
                    (payload["tenant_id"], payload["detection_id"]),
                ).fetchone()
                if candidate_row is None:
                    raise VideoPipelineError("resource_not_found", "Candidate was not found")
                candidate = json.loads(candidate_row["payload_json"])
                candidate["review_status"] = payload["decision"]
                connection.execute(
                    """UPDATE candidate_detections_restricted SET payload_json=?
                       WHERE tenant_id=? AND detection_id=?""",
                    (_json(candidate), payload["tenant_id"], payload["detection_id"]),
                )
                connection.execute(
                    """INSERT INTO accepted_events (
                         tenant_id, source_id, external_event_id, occurred_at,
                         received_at, category, latitude, longitude, accuracy_meters,
                         source_sequence_json, attributes_json, event_hash, ingested_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event["tenant_id"], event["source_id"], event["external_event_id"],
                        event["occurred_at"], event["received_at"], event["category"],
                        location["latitude"], location["longitude"], location.get("accuracy_meters"),
                        json.dumps(event.get("source_sequence")),
                        json.dumps(event.get("attributes", {}), sort_keys=True),
                        event_hash, utc_now(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise VideoPipelineError(
                "review_already_final", "Candidate already has an immutable final review"
            ) from error

    def get_review_for_candidate(self, tenant_id: str, detection_id: str) -> dict[str, Any] | None:
        with self.ingestion_store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM candidate_reviews_restricted WHERE tenant_id=? AND detection_id=?",
                (tenant_id, detection_id),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def put_coverage(self, payload: dict[str, Any]) -> None:
        with self.ingestion_store.connect() as connection:
            connection.execute(
                """INSERT INTO coverage_snapshots VALUES (?, ?, ?, ?)
                   ON CONFLICT (tenant_id, source_id, interval_start) DO UPDATE SET payload_json=excluded.payload_json""",
                (payload["tenant_id"], payload["source_id"], payload["interval_start"], _json(payload)),
            )

    def list_coverage(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                """SELECT payload_json FROM coverage_snapshots WHERE tenant_id=?
                   ORDER BY interval_start DESC LIMIT ?""",
                (tenant_id, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def coverage_ratio(self, tenant_id: str, source_ids: tuple[str, ...], interval_start: str) -> float:
        if not source_ids:
            raise ValueError("At least one source is required")
        placeholders = ",".join("?" for _ in source_ids)
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM coverage_snapshots WHERE tenant_id=? AND source_id IN ({placeholders}) AND interval_start=?",  # nosec B608
                (tenant_id, *source_ids, interval_start),
            ).fetchall()
        if len(rows) != len(set(source_ids)):
            raise VideoPipelineError("coverage_missing", "Measured coverage is missing for one or more sources")
        values = [json.loads(row["payload_json"]) for row in rows]
        expected = sum(value["expected_seconds"] for value in values)
        available = sum(value["detector_available_seconds"] for value in values)
        return available / expected

    def latest_coverage_ratio(self, tenant_id: str, source_ids: tuple[str, ...], before: str) -> float:
        """Return an expected-seconds-weighted ratio from the latest completed snapshot per source."""
        values: list[dict[str, Any]] = []
        with self.ingestion_store.connect() as connection:
            for source_id in source_ids:
                rows = connection.execute(
                    "SELECT payload_json FROM coverage_snapshots WHERE tenant_id=? AND source_id=? ORDER BY interval_start DESC",
                    (tenant_id, source_id),
                ).fetchall()
                value = next(
                    (item for row in rows if (item := json.loads(row["payload_json"]))["interval_end"] <= before),
                    None,
                )
                if value is None:
                    raise VideoPipelineError("coverage_missing", "No completed measured coverage exists for a source")
                values.append(value)
        expected = sum(value["expected_seconds"] for value in values)
        available = sum(value["detector_available_seconds"] for value in values)
        return available / expected

    def latest_tenant_coverage_ratio(self, tenant_id: str, before: str) -> float:
        return self.latest_coverage_ratio(tenant_id, self.list_source_ids(tenant_id), before)

    def put_future_snapshot(self, tenant_id: str, version: str, interval_start: str, rows: list[dict[str, Any]]) -> None:
        with self.ingestion_store.connect() as connection:
            connection.execute(
                """INSERT INTO future_feature_snapshots VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (tenant_id, snapshot_version) DO UPDATE SET rows_json=excluded.rows_json""",
                (tenant_id, version, interval_start, _json(rows), utc_now()),
            )

    def expired_assets(self, now: str) -> list[tuple[str, str]]:
        with self.ingestion_store.connect() as connection:
            rows = connection.execute("SELECT tenant_id, asset_id, payload_json FROM video_assets_restricted").fetchall()
        return [
            (row["tenant_id"], row["asset_id"])
            for row in rows
            if json.loads(row["payload_json"])["retention_until"] <= now
            and json.loads(row["payload_json"])["status"] != "deleted"
        ]

    def audit(self, tenant_id: str, action: str, resource_type: str, resource_id: str, outcome: str, error_code: str | None = None) -> None:
        with self.ingestion_store.connect() as connection:
            connection.execute(
                "INSERT INTO video_audit_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, str(uuid.uuid4()), action, resource_type, resource_id, outcome, error_code, utc_now()),
            )

    def job_metrics(self, tenant_id: str) -> dict[str, int]:
        with self.ingestion_store.connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM video_processing_jobs WHERE tenant_id=? GROUP BY state", (tenant_id,)
            ).fetchall()
        return {row["state"]: int(row["count"]) for row in rows}

    def _get_payload(self, table: str, tenant_id: str, key_name: str, key: str) -> dict[str, Any]:
        allowed = {
            "camera_sources_restricted": "source_id",
            "video_assets_restricted": "asset_id",
            "candidate_detections_restricted": "detection_id",
        }
        if allowed.get(table) != key_name:
            raise ValueError("Unsupported restricted lookup")
        with self.ingestion_store.connect() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE tenant_id=? AND {key_name}=?",  # nosec B608
                (tenant_id, key),
            ).fetchone()
        if row is None:
            raise VideoPipelineError("resource_not_found", "Tenant-scoped resource was not found")
        return json.loads(row["payload_json"])

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        encoded = result.pop("safe_diagnostics_json", "{}")
        result["safe_diagnostics"] = json.loads(encoded or "{}")
        return result
