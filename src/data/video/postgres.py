"""PostgreSQL repository for tenant-scoped video pipeline state."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.data.postgres import PostgresIngestionStore, TenantPostgres, _time
from src.data.store import utc_now

from .errors import VideoPipelineError


class PostgresVideoStore:
    """Production video store; every query is protected by PostgreSQL RLS."""

    def __init__(self, database: TenantPostgres, ingestion_store: PostgresIngestionStore) -> None:
        self.database = database
        self.ingestion_store = ingestion_store

    def put_source(self, payload: dict[str, Any]) -> None:
        with self.database.transaction(payload["tenant_id"]) as cursor:
            cursor.execute(
                """INSERT INTO camera_sources (tenant_id, source_id, definition, created_at, updated_at)
                   VALUES (%s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (tenant_id, source_id) DO UPDATE
                   SET definition=excluded.definition, updated_at=excluded.updated_at""",
                (
                    payload["tenant_id"], payload["source_id"], json.dumps(payload),
                    payload["created_at"], utc_now(),
                ),
            )

    def get_source(self, tenant_id: str, source_id: str) -> dict[str, Any]:
        return self._get_json(tenant_id, "camera_sources", "definition", "source_id", source_id)

    def list_source_ids(self, tenant_id: str) -> tuple[str, ...]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                "SELECT source_id FROM camera_sources WHERE tenant_id=%s ORDER BY created_at, source_id",
                (tenant_id,),
            )
            return tuple(str(row["source_id"]) for row in cursor.fetchall())

    def put_asset(self, payload: dict[str, Any], storage_ref: Path | str) -> None:
        reference = str(storage_ref)
        with self.database.transaction(payload["tenant_id"]) as cursor:
            cursor.execute(
                """INSERT INTO video_assets_restricted
                   (tenant_id, asset_id, source_id, metadata, local_storage_ref, created_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (tenant_id, asset_id) DO UPDATE
                   SET metadata=excluded.metadata, local_storage_ref=excluded.local_storage_ref""",
                (
                    payload["tenant_id"], payload["asset_id"], payload["source_id"],
                    json.dumps(payload), reference, utc_now(),
                ),
            )

    def get_asset(self, tenant_id: str, asset_id: str) -> dict[str, Any]:
        return self._get_json(
            tenant_id, "video_assets_restricted", "metadata", "asset_id", asset_id,
            error_code="asset_not_found",
        )

    def asset_storage_ref(self, tenant_id: str, asset_id: str) -> str:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT local_storage_ref FROM video_assets_restricted
                   WHERE tenant_id=%s AND asset_id=%s""",
                (tenant_id, asset_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise VideoPipelineError("asset_not_found", "Video asset was not found")
        return str(row["local_storage_ref"])

    def asset_path(self, tenant_id: str, asset_id: str) -> Path:
        reference = self.asset_storage_ref(tenant_id, asset_id)
        if reference.startswith("file://"):
            return Path(reference[7:])
        if reference.startswith("secret://"):
            raise VideoPipelineError(
                "asset_materialization_required",
                "Object-backed media must be materialized through MediaStorage",
            )
        return Path(reference)

    def update_asset_status(
        self, tenant_id: str, asset_id: str, status: str, failure_code: str | None = None
    ) -> None:
        payload = self.get_asset(tenant_id, asset_id)
        payload["status"] = status
        if failure_code:
            payload["failure_code"] = failure_code
        else:
            payload.pop("failure_code", None)
        self.put_asset(payload, self.asset_storage_ref(tenant_id, asset_id))

    def tenant_asset_bytes(self, tenant_id: str) -> int:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT COALESCE(sum((metadata->>'size_bytes')::bigint), 0) AS size
                   FROM video_assets_restricted
                   WHERE tenant_id=%s AND metadata->>'status' <> 'deleted'""",
                (tenant_id,),
            )
            row = cursor.fetchone()
        return int(row["size"])

    def put_mapping(
        self, tenant_id: str, source_id: str, asset_id: str, reka_video_id: str, status: str
    ) -> None:
        now = utc_now()
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO reka_video_registry_restricted
                   (tenant_id, asset_id, source_id, reka_video_id, indexing_status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, asset_id) DO UPDATE
                   SET indexing_status=excluded.indexing_status, updated_at=excluded.updated_at""",
                (tenant_id, asset_id, source_id, reka_video_id, status, now, now),
            )

    def get_mapping(self, tenant_id: str, asset_id: str) -> dict[str, Any] | None:
        return self._get_row_or_none(
            tenant_id, "reka_video_registry_restricted", "asset_id", asset_id
        )

    def mapping_by_remote_id(self, tenant_id: str, reka_video_id: str) -> dict[str, Any] | None:
        return self._get_row_or_none(
            tenant_id, "reka_video_registry_restricted", "reka_video_id", reka_video_id
        )

    def mark_remote_deleted(self, tenant_id: str, asset_id: str) -> None:
        now = utc_now()
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE reka_video_registry_restricted
                   SET remote_deleted_at=%s, updated_at=%s
                   WHERE tenant_id=%s AND asset_id=%s""",
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
        job_id = str(uuid.uuid4())
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO video_processing_jobs
                   (tenant_id, job_id, asset_id, operation, idempotency_key, state,
                    attempts, max_attempts, available_at, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, 'queued', 0, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (tenant_id, job_id, asset_id, operation, key, max_attempts, now, now, now),
            )
            cursor.execute(
                """SELECT * FROM video_processing_jobs
                   WHERE tenant_id=%s AND idempotency_key=%s""",
                (tenant_id, key),
            )
            row = cursor.fetchone()
            if row is None:
                raise VideoPipelineError(
                    "job_active_conflict", "Equivalent processing work is already active"
                )
        return self._job(row)

    def get_job(self, tenant_id: str, job_id: str) -> dict[str, Any]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                "SELECT * FROM video_processing_jobs WHERE tenant_id=%s AND job_id=%s",
                (tenant_id, job_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise VideoPipelineError("job_not_found", "Processing job was not found")
        return self._job(row)

    def list_jobs(self, tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM video_processing_jobs WHERE tenant_id=%s
                   ORDER BY created_at DESC LIMIT %s""",
                (tenant_id, min(max(limit, 1), 100)),
            )
            return [self._job(row) for row in cursor.fetchall()]

    def jobs_for_asset(self, tenant_id: str, asset_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM video_processing_jobs
                   WHERE tenant_id=%s AND asset_id=%s
                   ORDER BY created_at, job_id""",
                (tenant_id, asset_id),
            )
            return [self._job(row) for row in cursor.fetchall()]

    def claim_job(
        self, tenant_id: str, job_id: str, *, worker_id: str, lease_seconds: int
    ) -> dict[str, Any]:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=lease_seconds)
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE video_processing_jobs SET state='running', attempts=attempts+1,
                   started_at=COALESCE(started_at, %s), heartbeat_at=%s, lease_expires_at=%s,
                   worker_id=%s, updated_at=%s
                   WHERE tenant_id=%s AND job_id=%s
                     AND state IN ('queued','retry') AND available_at <= %s
                     AND attempts < max_attempts
                   RETURNING *""",
                (now, now, lease, worker_id, now, tenant_id, job_id, now),
            )
            row = cursor.fetchone()
        if row is None:
            raise VideoPipelineError("job_not_claimable", "Processing job is not ready for this worker")
        return self._job(row)

    def heartbeat(
        self, tenant_id: str, job_id: str, *, worker_id: str, lease_seconds: int
    ) -> None:
        now = datetime.now(UTC)
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE video_processing_jobs SET heartbeat_at=%s,
                   lease_expires_at=%s, updated_at=%s
                   WHERE tenant_id=%s AND job_id=%s AND state='running' AND worker_id=%s""",
                (
                    now, now + timedelta(seconds=lease_seconds), now,
                    tenant_id, job_id, worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise VideoPipelineError("job_lease_lost", "Worker no longer owns the processing lease")

    def extend_index_attempt_limit(
        self, tenant_id: str, job_id: str, *, max_attempts: int
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE video_processing_jobs SET max_attempts=%s, updated_at=%s
                   WHERE tenant_id=%s AND job_id=%s AND operation='index'
                     AND state='retry' AND last_error_code='reka_index_pending'
                     AND attempts >= max_attempts AND max_attempts < %s
                   RETURNING *""",
                (max_attempts, now, tenant_id, job_id, max_attempts),
            )
            row = cursor.fetchone()
        if row is None:
            raise VideoPipelineError(
                "job_attempt_limit_not_extendable",
                "Only an exhausted pending index job can receive the configured bound",
            )
        return self._job(row)

    def mark_dead_lettered(self, tenant_id: str, job_id: str) -> None:
        now = datetime.now(UTC)
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE video_processing_jobs SET dead_lettered_at=%s, updated_at=%s
                   WHERE tenant_id=%s AND job_id=%s AND state='failed'""",
                (now, now, tenant_id, job_id),
            )
            if cursor.rowcount != 1:
                raise VideoPipelineError(
                    "job_not_failed", "Only failed jobs can enter the dead-letter queue"
                )

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
        now = datetime.now(UTC)
        available = now + timedelta(seconds=max(retry_delay_seconds, 0))
        finished = now if state in {"completed", "failed", "cancelled"} else None
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE video_processing_jobs SET state=%s, last_error_code=%s,
                   safe_diagnostics=%s::jsonb,
                   available_at=%s, finished_at=%s, lease_expires_at=NULL,
                   heartbeat_at=NULL, worker_id=NULL, updated_at=%s
                   WHERE tenant_id=%s AND job_id=%s RETURNING *""",
                (
                    state,
                    error_code,
                    json.dumps(safe_diagnostics or {}),
                    available,
                    finished,
                    now,
                    tenant_id,
                    job_id,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise VideoPipelineError("job_not_found", "Processing job was not found")
        return self._job(row)

    def recover_stale_jobs(
        self, *, tenant_id: str, stale_after: timedelta | None = None, now: datetime | None = None
    ) -> int:
        current = now or datetime.now(UTC)
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """UPDATE video_processing_jobs SET
                   state=CASE WHEN attempts < max_attempts THEN 'retry' ELSE 'failed' END,
                   last_error_code=CASE WHEN attempts < max_attempts
                     THEN 'worker_lease_expired' ELSE 'worker_recovery_exhausted' END,
                   available_at=%s, lease_expires_at=NULL, heartbeat_at=NULL,
                   worker_id=NULL, finished_at=CASE WHEN attempts >= max_attempts THEN %s END,
                   updated_at=%s
                   WHERE tenant_id=%s AND state='running' AND lease_expires_at < %s""",
                (current, current, current, tenant_id, current),
            )
            return cursor.rowcount

    def put_candidate(self, payload: dict[str, Any], semantic_key: str) -> bool:
        with self.database.transaction(payload["tenant_id"]) as cursor:
            cursor.execute(
                """INSERT INTO candidate_detections_restricted
                   (tenant_id, detection_id, source_id, asset_id, semantic_key, candidate, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                   ON CONFLICT (tenant_id, semantic_key) DO NOTHING""",
                (
                    payload["tenant_id"], payload["detection_id"], payload["source_id"],
                    payload["asset_id"], semantic_key, json.dumps(payload), utc_now(),
                ),
            )
            return cursor.rowcount == 1

    def get_candidate(self, tenant_id: str, detection_id: str) -> dict[str, Any]:
        return self._get_json(
            tenant_id, "candidate_detections_restricted", "candidate",
            "detection_id", detection_id,
        )

    def list_candidates(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT candidate FROM candidate_detections_restricted
                   WHERE tenant_id=%s ORDER BY created_at""",
                (tenant_id,),
            )
            return [dict(row["candidate"]) for row in cursor.fetchall()]

    def delete_pending_candidates(self, tenant_id: str) -> int:
        """Delete unreviewed demo-session candidates while preserving final reviews."""
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """DELETE FROM candidate_detections_restricted AS candidate_row
                   WHERE candidate_row.tenant_id=%s
                     AND candidate_row.candidate->>'review_status'='awaiting_review'
                     AND NOT EXISTS (
                       SELECT 1 FROM candidate_reviews_restricted AS review_row
                       WHERE review_row.tenant_id=candidate_row.tenant_id
                         AND review_row.detection_id=candidate_row.detection_id
                     )""",
                (tenant_id,),
            )
            return cursor.rowcount

    def update_candidate(self, payload: dict[str, Any]) -> None:
        with self.database.transaction(payload["tenant_id"]) as cursor:
            cursor.execute(
                """UPDATE candidate_detections_restricted SET candidate=%s::jsonb
                   WHERE tenant_id=%s AND detection_id=%s""",
                (json.dumps(payload), payload["tenant_id"], payload["detection_id"]),
            )
            if cursor.rowcount != 1:
                raise VideoPipelineError("resource_not_found", "Tenant-scoped candidate was not found")

    def put_review(self, payload: dict[str, Any]) -> None:
        with self.database.transaction(payload["tenant_id"]) as cursor:
            cursor.execute(
                """INSERT INTO candidate_reviews_restricted
                   (tenant_id, review_id, detection_id, decision, created_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s)
                   ON CONFLICT (tenant_id, detection_id) DO NOTHING""",
                (
                    payload["tenant_id"], payload["review_id"], payload["detection_id"],
                    json.dumps(payload), utc_now(),
                ),
            )
            if cursor.rowcount != 1:
                raise VideoPipelineError("review_already_final", "Candidate already has a final review")
            cursor.execute(
                """UPDATE candidate_detections_restricted
                   SET candidate=jsonb_set(candidate, '{review_status}', to_jsonb(%s::text))
                   WHERE tenant_id=%s AND detection_id=%s""",
                (payload["decision"], payload["tenant_id"], payload["detection_id"]),
            )

    def put_review_and_event(
        self, payload: dict[str, Any], event: dict[str, Any], event_hash: str
    ) -> None:
        """Atomically finalize a confirmed review and its canonical event."""
        tenant_id = payload["tenant_id"]
        location = event["location"]
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO candidate_reviews_restricted
                   (tenant_id, review_id, detection_id, decision, created_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s)
                   ON CONFLICT (tenant_id, detection_id) DO NOTHING""",
                (
                    tenant_id, payload["review_id"], payload["detection_id"],
                    json.dumps(payload), utc_now(),
                ),
            )
            if cursor.rowcount != 1:
                raise VideoPipelineError(
                    "review_already_final", "Candidate already has an immutable final review"
                )
            cursor.execute(
                """UPDATE candidate_detections_restricted
                   SET candidate=jsonb_set(candidate, '{review_status}', to_jsonb(%s::text))
                   WHERE tenant_id=%s AND detection_id=%s""",
                (payload["decision"], tenant_id, payload["detection_id"]),
            )
            if cursor.rowcount != 1:
                raise VideoPipelineError("resource_not_found", "Candidate was not found")
            cursor.execute(
                """INSERT INTO accepted_incident_events_restricted
                   (tenant_id, source_id, external_event_id, event, event_hash,
                    occurred_at, received_at, category, latitude, longitude, ingested_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, source_id, external_event_id) DO NOTHING""",
                (
                    tenant_id, event["source_id"], event["external_event_id"], json.dumps(event),
                    event_hash, event["occurred_at"], event["received_at"], event["category"],
                    location["latitude"], location["longitude"], utc_now(),
                ),
            )
            if cursor.rowcount != 1:
                raise VideoPipelineError(
                    "event_promotion_conflict", "Confirmed event promotion was not unique"
                )

    def get_review_for_candidate(self, tenant_id: str, detection_id: str) -> dict[str, Any] | None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT decision FROM candidate_reviews_restricted
                   WHERE tenant_id=%s AND detection_id=%s""",
                (tenant_id, detection_id),
            )
            row = cursor.fetchone()
        return dict(row["decision"]) if row else None

    def put_coverage(self, payload: dict[str, Any]) -> None:
        with self.database.transaction(payload["tenant_id"]) as cursor:
            cursor.execute(
                """INSERT INTO coverage_snapshots
                   (tenant_id, source_id, interval_start, interval_end, snapshot)
                   VALUES (%s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (tenant_id, source_id, interval_start) DO UPDATE
                   SET interval_end=excluded.interval_end, snapshot=excluded.snapshot""",
                (
                    payload["tenant_id"], payload["source_id"], payload["interval_start"],
                    payload["interval_end"], json.dumps(payload),
                ),
            )

    def list_coverage(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT snapshot FROM coverage_snapshots WHERE tenant_id=%s
                   ORDER BY interval_start DESC LIMIT %s""",
                (tenant_id, limit),
            )
            return [dict(row["snapshot"]) for row in cursor.fetchall()]

    def coverage_ratio(self, tenant_id: str, source_ids: tuple[str, ...], interval_start: str) -> float:
        values = self._coverage_values(tenant_id, source_ids, interval_start=interval_start)
        return _weighted_coverage(values, source_ids)

    def latest_coverage_ratio(self, tenant_id: str, source_ids: tuple[str, ...], before: str) -> float:
        if not source_ids:
            raise ValueError("At least one source is required")
        values: list[dict[str, Any]] = []
        with self.database.transaction(tenant_id) as cursor:
            for source_id in source_ids:
                cursor.execute(
                    """SELECT snapshot FROM coverage_snapshots
                       WHERE tenant_id=%s AND source_id=%s AND interval_end <= %s
                       ORDER BY interval_end DESC LIMIT 1""",
                    (tenant_id, source_id, before),
                )
                row = cursor.fetchone()
                if row:
                    values.append(dict(row["snapshot"]))
        return _weighted_coverage(values, source_ids)

    def latest_tenant_coverage_ratio(self, tenant_id: str, before: str) -> float:
        return self.latest_coverage_ratio(tenant_id, self.list_source_ids(tenant_id), before)

    def put_future_snapshot(
        self, tenant_id: str, version: str, interval_start: str, rows: list[dict[str, Any]]
    ) -> None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO future_feature_snapshots
                   (tenant_id, snapshot_version, interval_start, rows, created_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s)
                   ON CONFLICT (tenant_id, snapshot_version) DO NOTHING""",
                (tenant_id, version, interval_start, json.dumps(rows), utc_now()),
            )

    def expired_assets(self, now: str, *, tenant_id: str) -> list[tuple[str, str]]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT asset_id FROM video_assets_restricted
                   WHERE tenant_id=%s AND metadata->>'status' <> 'deleted'
                     AND (metadata->>'retention_until')::timestamptz <= %s""",
                (tenant_id, now),
            )
            return [(tenant_id, str(row["asset_id"])) for row in cursor.fetchall()]

    def audit(
        self,
        tenant_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO video_audit_log
                   (tenant_id, audit_id, action, resource_type, resource_id, outcome, error_code, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tenant_id, str(uuid.uuid4()), action, resource_type,
                    resource_id, outcome, error_code, utc_now(),
                ),
            )

    def job_metrics(self, tenant_id: str) -> dict[str, int]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT state, count(*) AS count FROM video_processing_jobs
                   WHERE tenant_id=%s GROUP BY state""",
                (tenant_id,),
            )
            return {row["state"]: int(row["count"]) for row in cursor.fetchall()}

    def _coverage_values(
        self, tenant_id: str, source_ids: tuple[str, ...], *, interval_start: str
    ) -> list[dict[str, Any]]:
        if not source_ids:
            raise ValueError("At least one source is required")
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT snapshot FROM coverage_snapshots
                   WHERE tenant_id=%s AND source_id=ANY(%s::uuid[]) AND interval_start=%s""",
                (tenant_id, list(source_ids), interval_start),
            )
            return [dict(row["snapshot"]) for row in cursor.fetchall()]

    def _get_row_or_none(
        self, tenant_id: str, table: str, key_name: str, key: str
    ) -> dict[str, Any] | None:
        allowed = {
            ("reka_video_registry_restricted", "asset_id"),
            ("reka_video_registry_restricted", "reka_video_id"),
        }
        if (table, key_name) not in allowed:
            raise ValueError("Unsupported restricted lookup")
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                f"SELECT * FROM {table} WHERE tenant_id=%s AND {key_name}=%s",  # nosec B608
                (tenant_id, key),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {name: _time(value) if name.endswith("_at") else str(value) if name.endswith("_id") else value for name, value in row.items()}

    def _get_json(
        self,
        tenant_id: str,
        table: str,
        column: str,
        key_name: str,
        key: str,
        *,
        error_code: str = "resource_not_found",
    ) -> dict[str, Any]:
        allowed = {
            ("camera_sources", "definition", "source_id"),
            ("video_assets_restricted", "metadata", "asset_id"),
            ("candidate_detections_restricted", "candidate", "detection_id"),
        }
        if (table, column, key_name) not in allowed:
            raise ValueError("Unsupported restricted lookup")
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                f"SELECT {column} FROM {table} WHERE tenant_id=%s AND {key_name}=%s",  # nosec B608
                (tenant_id, key),
            )
            row = cursor.fetchone()
        if row is None:
            raise VideoPipelineError(error_code, "Tenant-scoped resource was not found")
        return dict(row[column])

    @staticmethod
    def _job(row: dict[str, Any]) -> dict[str, Any]:
        return {
            name: (_time(value) if name.endswith("_at") else str(value) if name.endswith("_id") else value)
            for name, value in row.items()
        }


def _weighted_coverage(values: list[dict[str, Any]], source_ids: tuple[str, ...]) -> float:
    if len(values) != len(set(source_ids)):
        raise VideoPipelineError("coverage_missing", "Measured coverage is missing for one or more sources")
    expected = sum(int(value["expected_seconds"]) for value in values)
    available = sum(int(value["detector_available_seconds"]) for value in values)
    if expected <= 0:
        raise VideoPipelineError("coverage_invalid", "Measured coverage has no expected duration")
    return available / expected
