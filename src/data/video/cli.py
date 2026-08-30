"""Commands for migrations and isolated durable video workers."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

from .capture import AwsSecretsManagerResolver, FfmpegSegmenter, LiveCaptureWorker
from .coverage import PostgresCoverageTelemetry
from .errors import VideoPipelineError
from .runtime import PlatformSettings, _secret_value, create_platform_runtime
from .worker import VideoJobWorker


class WorkerLocationResolver:
    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]:
        raise VideoPipelineError(
            "location_resolution_unavailable",
            "Review promotion requires the API's secret-backed location resolver",
        )


def _migration_database_url() -> str:
    database_url = _secret_value("DATABASE_MIGRATOR_URL")
    if not database_url:
        raise ValueError("DATABASE_MIGRATOR_URL is required")
    return database_url


def _assert_migrator_connection(cursor: Any) -> None:
    expected_role = os.getenv("DATABASE_MIGRATOR_ROLE", "crime_migrator").strip()
    if expected_role != "crime_migrator":
        raise ValueError("DATABASE_MIGRATOR_ROLE must be crime_migrator")
    cursor.execute(
        """SELECT rolname, session_user, rolsuper, rolcreatedb, rolcreaterole, rolinherit,
                  rolbypassrls,
                  has_database_privilege(rolname, current_database(), 'CREATE'),
                  has_schema_privilege(rolname, 'public', 'CREATE'),
                  NOT EXISTS (
                    SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid
                  )
           FROM pg_roles WHERE rolname = current_user"""
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Migration database role could not be verified")
    (
        role_name,
        session_role,
        is_superuser,
        can_create_database,
        can_create_role,
        inherits_roles,
        bypasses_rls,
        can_create_schema,
        can_create_in_public,
        has_no_memberships,
    ) = row
    if role_name != expected_role or session_role != expected_role:
        raise RuntimeError("Migrations require the dedicated crime_migrator role")
    if any(
        (
            is_superuser,
            can_create_database,
            can_create_role,
            inherits_roles,
            bypasses_rls,
        )
    ):
        raise RuntimeError("The migration role has unsafe PostgreSQL attributes")
    if not can_create_schema or not can_create_in_public:
        raise RuntimeError("The migration role is missing required DDL privileges")
    if not has_no_memberships:
        raise RuntimeError("The migration role must not inherit or SET ROLE")


def migrate() -> None:
    database_url = _migration_database_url()
    try:
        import psycopg
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("Install the platform extra: pip install -e '.[platform]'") from error
    root = Path(__file__).resolve().parents[3]
    migrations = sorted((root / "migrations" / "postgres").glob("*.sql"))
    with (
        psycopg.connect(database_url, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        _assert_migrator_connection(cursor)
        for migration in migrations:
            cursor.execute(migration.read_text(encoding="utf-8"))


def worker() -> None:
    parser = argparse.ArgumentParser(description="Run one isolated video worker stage")
    parser.add_argument(
        "--operation", choices=("upload", "index", "analyze", "delete"), required=True
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    settings = PlatformSettings.from_environment()
    runtime = create_platform_runtime(settings, location_resolver=WorkerLocationResolver())
    processor = VideoJobWorker(
        store=runtime.video_store,
        broker=runtime.broker,
        service=runtime.service,
        operations=(args.operation,),
        lease_seconds=settings.worker_lease_seconds,
        telemetry=PostgresCoverageTelemetry(runtime.database),
    )
    try:
        while True:
            if not processor.poll_once():
                time.sleep(max(args.poll_seconds, 0.1))
    finally:
        runtime.close()


def capture_worker() -> None:
    parser = argparse.ArgumentParser(description="Run one tenant-scoped live capture worker")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--endpoint-secret-id", required=True)
    parser.add_argument("--credential-secret-id", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = PlatformSettings.from_environment()
    runtime = create_platform_runtime(settings, location_resolver=WorkerLocationResolver())
    try:
        source = runtime.video_store.get_source(args.tenant_id, args.source_id)
        connection = source["connection"]
        secrets = AwsSecretsManagerResolver(
            reference_map={
                connection["endpoint_ref"]: args.endpoint_secret_id,
                connection["credential_ref"]: args.credential_secret_id,
            },
            region_name=settings.aws_region,
        )
        capture = LiveCaptureWorker(
            store=runtime.video_store,
            service=runtime.service,
            broker=runtime.broker,
            secrets=secrets,
            telemetry=PostgresCoverageTelemetry(runtime.database),
            segmenter=FfmpegSegmenter(
                max_output_bytes=settings.max_upload_bytes,
            ),
            spool_root=settings.restricted_spool_root,
            segment_seconds=int(os.getenv("VIDEO_SEGMENT_SECONDS", "30")),
            max_pending_segments=int(os.getenv("VIDEO_MAX_PENDING_SEGMENTS", "20")),
        )
        while True:
            capture.capture_once(args.tenant_id, args.source_id)
            if args.once:
                break
    finally:
        runtime.close()


def demo_worker() -> None:
    """Run one worker stage against the self-contained Postgres demo broker."""
    parser = argparse.ArgumentParser(description="Run one integrated-demo worker stage")
    parser.add_argument(
        "--operation", choices=("upload", "index", "analyze", "delete"), required=True
    )
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args()
    from src.api.settings import Settings

    from .demo_runtime import create_demo_runtime

    runtime = create_demo_runtime()
    api_settings = Settings.from_environment()
    processor = VideoJobWorker(
        store=runtime.video_store,
        broker=runtime.broker,
        service=runtime.service,
        operations=(args.operation,),
        lease_seconds=120,
        telemetry=PostgresCoverageTelemetry(runtime.database),
        index_max_attempts=api_settings.reka_index_max_polls,
        index_poll_seconds=max(0, round(api_settings.reka_index_poll_seconds)),
    )
    try:
        while True:
            if not processor.poll_once():
                time.sleep(max(args.poll_seconds, 0.1))
    finally:
        runtime.close()
