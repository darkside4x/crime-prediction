from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

MIGRATOR_DSN = os.getenv("TEST_POSTGRES_MIGRATOR_DSN")
RUNTIME_DSN = os.getenv("TEST_POSTGRES_RUNTIME_DSN")
pytestmark = pytest.mark.skipif(
    not MIGRATOR_DSN or not RUNTIME_DSN,
    reason=(
        "Set TEST_POSTGRES_MIGRATOR_DSN and TEST_POSTGRES_RUNTIME_DSN to run "
        "direct PostgreSQL RLS integration tests"
    ),
)


def test_database_rls_denies_known_cross_tenant_id() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.postgres import TenantPostgres

    root = Path(__file__).resolve().parents[2]
    with (
        psycopg.connect(MIGRATOR_DSN, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            cursor.execute(migration.read_text(encoding="utf-8"))

    tenant_a = "11111111-1111-4111-8111-111111111111"
    tenant_b = "22222222-2222-4222-8222-222222222222"
    source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    database = TenantPostgres(RUNTIME_DSN)
    try:
        with database.transaction(tenant_a) as cursor:
            cursor.execute(
                """INSERT INTO camera_sources (tenant_id, source_id, definition)
                   VALUES (%s, %s, '{}'::jsonb)
                   ON CONFLICT (tenant_id, source_id) DO NOTHING""",
                (tenant_a, source_id),
            )
            cursor.execute(
                "SELECT source_id FROM camera_sources WHERE tenant_id=%s AND source_id=%s",
                (tenant_a, source_id),
            )
            assert cursor.fetchone() is not None

        with database.transaction(tenant_b) as cursor:
            cursor.execute(
                "SELECT source_id FROM camera_sources WHERE source_id=%s", (source_id,)
            )
            assert cursor.fetchone() is None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "INSERT INTO camera_sources (tenant_id, source_id, definition) VALUES (%s, %s, '{}'::jsonb)",
                    (tenant_a, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                )
    finally:
        database.close()


def test_demo_job_queue_denies_two_tenant_direct_access_but_workers_can_claim() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.postgres import TenantPostgres
    from src.data.video.broker import JobMessage, PostgresJobBroker

    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(MIGRATOR_DSN, autocommit=True) as connection:
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))

    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    jobs: dict[str, str] = {}
    database = TenantPostgres(RUNTIME_DSN)
    broker = PostgresJobBroker(database)
    try:
        for tenant_id in (tenant_a, tenant_b):
            source_id = str(uuid.uuid4())
            asset_id = str(uuid.uuid4())
            job_id = str(uuid.uuid4())
            jobs[tenant_id] = job_id
            with database.transaction(tenant_id) as cursor:
                cursor.execute(
                    """INSERT INTO camera_sources
                       (tenant_id,source_id,definition)
                       VALUES (%s,%s,'{}'::jsonb)""",
                    (tenant_id, source_id),
                )
                cursor.execute(
                    """INSERT INTO video_assets_restricted
                       (tenant_id,asset_id,source_id,metadata)
                       VALUES (%s,%s,%s,'{}'::jsonb)""",
                    (tenant_id, asset_id, source_id),
                )
                cursor.execute(
                    """INSERT INTO video_processing_jobs
                       (tenant_id,job_id,asset_id,operation,idempotency_key,state,
                        max_attempts)
                       VALUES (%s,%s,%s,'delete',%s,'queued',3)""",
                    (tenant_id, job_id, asset_id, f"queue-rls:{job_id}"),
                )
            broker.publish(JobMessage(tenant_id, job_id, "delete"))

        with database.transaction(tenant_a) as cursor:
            cursor.execute(
                "SELECT tenant_id,job_id FROM demo_job_messages ORDER BY tenant_id"
            )
            assert [str(row["tenant_id"]) for row in cursor.fetchall()] == [tenant_a]
            cursor.execute(
                """UPDATE demo_job_messages SET error_code='cross_tenant_write'
                   WHERE tenant_id=%s AND job_id=%s""",
                (tenant_b, jobs[tenant_b]),
            )
            assert cursor.rowcount == 0
            cursor.execute(
                "DELETE FROM demo_job_messages WHERE tenant_id=%s AND job_id=%s",
                (tenant_b, jobs[tenant_b]),
            )
            assert cursor.rowcount == 0

        with database.transaction(tenant_b) as cursor:
            cursor.execute(
                "SELECT error_code FROM demo_job_messages WHERE job_id=%s",
                (jobs[tenant_b],),
            )
            assert cursor.fetchone()["error_code"] is None
            cursor.execute(
                "SELECT job_id FROM demo_job_messages WHERE job_id=%s",
                (jobs[tenant_a],),
            )
            assert cursor.fetchone() is None

        deliveries = broker.receive(operations=("delete",), limit=10)
        claimed = {
            delivery.message.job_id: delivery
            for delivery in deliveries
            if delivery.message.job_id in jobs.values()
        }
        assert set(claimed) == set(jobs.values())
        for delivery in claimed.values():
            broker.acknowledge(delivery)
        with database.transaction(tenant_a) as cursor:
            cursor.execute(
                "SELECT job_id FROM demo_job_messages WHERE job_id=%s",
                (jobs[tenant_a],),
            )
            assert cursor.fetchone() is None
        with database.transaction(tenant_b) as cursor:
            cursor.execute(
                "SELECT job_id FROM demo_job_messages WHERE job_id=%s",
                (jobs[tenant_b],),
            )
            assert cursor.fetchone() is None
    finally:
        database.close()


def test_database_rate_limit_is_atomic_and_stores_no_raw_key() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.platform_security import PostgresRateLimiter
    from src.data.postgres import TenantPostgres

    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(MIGRATOR_DSN, autocommit=True) as connection:
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))

    database = TenantPostgres(RUNTIME_DSN)
    try:
        limiter = PostgresRateLimiter(database, requests=2, window_seconds=60)
        raw_key = f"token:raw-credential-must-not-be-stored-{uuid.uuid4()}"
        assert limiter.allow(raw_key, now=120.0) == (True, 0)
        assert limiter.allow(raw_key, now=121.0) == (True, 0)
        assert limiter.allow(raw_key, now=122.0) == (False, 58)
        with database.system_transaction() as cursor:
            cursor.execute(
                "SELECT key_hash,request_count FROM api_rate_limit_buckets WHERE window_start=120"
            )
            row = cursor.fetchone()
        assert row["key_hash"] != raw_key
        assert len(row["key_hash"]) == 64
        assert row["request_count"] == 2
    finally:
        database.close()


def test_active_tenant_state_is_shared_without_principal_pii() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.platform_security import PostgresActiveTenantStore
    from src.data.postgres import TenantPostgres

    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(MIGRATOR_DSN, autocommit=True) as connection:
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))

    database = TenantPostgres(RUNTIME_DSN)
    try:
        first_replica = PostgresActiveTenantStore(database)
        second_replica = PostgresActiveTenantStore(database)
        principal = "oidc-user-review-two"
        tenant = "11111111-1111-4111-8111-111111111111"
        first_replica.set(principal, tenant)
        assert second_replica.get(principal) == tenant
        with database.system_transaction() as cursor:
            cursor.execute(
                "SELECT principal_hash,tenant_id FROM principal_active_tenants WHERE tenant_id=%s",
                (tenant,),
            )
            row = cursor.fetchone()
        assert row["principal_hash"] != principal
        assert len(row["principal_hash"]) == 64
    finally:
        database.close()


def test_video_job_safe_diagnostics_are_json_and_tenant_scoped() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.postgres import PostgresIngestionStore, TenantPostgres
    from src.data.video.errors import VideoPipelineError
    from src.data.video.postgres import PostgresVideoStore

    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(MIGRATOR_DSN, autocommit=True) as connection:
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))

    tenant_a = "11111111-1111-4111-8111-111111111111"
    tenant_b = "22222222-2222-4222-8222-222222222222"
    source_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    database = TenantPostgres(RUNTIME_DSN)
    store = PostgresVideoStore(database, PostgresIngestionStore(database))
    try:
        store.put_source(
            {
                "tenant_id": tenant_a,
                "source_id": source_id,
                "status": "active",
                "created_at": "2026-08-30T00:00:00Z",
            }
        )
        store.put_asset(
            {
                "tenant_id": tenant_a,
                "source_id": source_id,
                "asset_id": asset_id,
                "status": "processing",
            },
            f"file:///tmp/{asset_id}.mp4",
        )
        job = store.enqueue(tenant_a, asset_id, "analyze")
        failed = store.transition_job(
            tenant_a,
            job["job_id"],
            "failed",
            "reka_output_missing_fields",
            safe_diagnostics={
                "proposal_index": 0,
                "missing_fields": ["confidence"],
            },
        )
        assert failed["safe_diagnostics"] == {
            "proposal_index": 0,
            "missing_fields": ["confidence"],
        }
        fresh = store.enqueue(
            tenant_a,
            asset_id,
            "analyze",
            idempotency_key=f"reanalysis:{job['job_id']}:one",
        )
        assert fresh["state"] == "queued"
        with pytest.raises(VideoPipelineError) as active:
            store.enqueue(
                tenant_a,
                asset_id,
                "analyze",
                idempotency_key=f"reanalysis:{job['job_id']}:two",
            )
        assert active.value.code == "job_active_conflict"
        with pytest.raises(VideoPipelineError) as caught:
            store.get_job(tenant_b, job["job_id"])
        assert caught.value.code == "job_not_found"
    finally:
        database.close()
