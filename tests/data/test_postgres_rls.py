from __future__ import annotations

import os
from pathlib import Path

import pytest

DSN = os.getenv("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="Set TEST_POSTGRES_DSN to run direct PostgreSQL RLS integration tests",
)


def test_database_rls_denies_known_cross_tenant_id() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.postgres import TenantPostgres

    root = Path(__file__).resolve().parents[2]
    with (
        psycopg.connect(DSN, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            cursor.execute(migration.read_text(encoding="utf-8"))

    tenant_a = "11111111-1111-4111-8111-111111111111"
    tenant_b = "22222222-2222-4222-8222-222222222222"
    source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    database = TenantPostgres(DSN)
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


def test_database_rate_limit_is_atomic_and_stores_no_raw_key() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.platform_security import PostgresRateLimiter
    from src.data.postgres import TenantPostgres

    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(DSN, autocommit=True) as connection:
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))

    database = TenantPostgres(DSN)
    try:
        limiter = PostgresRateLimiter(database, requests=2, window_seconds=60)
        raw_key = "token:raw-credential-must-not-be-stored"
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
