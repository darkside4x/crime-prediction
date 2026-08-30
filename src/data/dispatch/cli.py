"""Command-line entry point for the isolated dispatch worker."""

from __future__ import annotations

import time

from src.api.state import PostgresAuditLog, PostgresIdempotencyStore
from src.data.postgres import TenantPostgres
from src.data.video.runtime import _secret_value

from .runtime import DispatchSettings, create_dispatch_runtime
from .worker import DispatchWorker


def worker() -> None:
    database_url = _secret_value("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    settings = DispatchSettings.from_environment()
    database = TenantPostgres(database_url)
    runtime = create_dispatch_runtime(
        settings,
        database=database,
        audit_log=PostgresAuditLog(database),
        idempotency_store=PostgresIdempotencyStore(database),
    )
    processor = DispatchWorker(
        broker=runtime.broker,
        coordinator=runtime.coordinator,
        mode=settings.mode,
    )
    try:
        while True:
            if not processor.poll_once():
                time.sleep(0.25)
    finally:
        database.close()


__all__ = ["worker"]
