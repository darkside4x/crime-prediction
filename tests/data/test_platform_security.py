from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from src.data.platform_security import PostgresRateLimiter


class RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []

    def execute(
        self, statement: str, parameters: tuple[Any, ...] | None = None
    ) -> None:
        self.statements.append((statement, parameters))

    def fetchone(self) -> dict[str, int]:
        return {"request_count": 1}


class RecordingDatabase:
    def __init__(self) -> None:
        self.cursor = RecordingCursor()

    @contextmanager
    def system_transaction(self) -> Iterator[RecordingCursor]:
        yield self.cursor


def test_periodic_rate_limit_cleanup_uses_the_injected_clock() -> None:
    database = RecordingDatabase()
    limiter = PostgresRateLimiter(database, requests=2, window_seconds=60)

    assert limiter.allow(
        "token:raw-credential-must-not-be-stored-182", now=122.0
    ) == (True, 0)

    assert len(database.cursor.statements) == 3
    cleanup_sql, cleanup_parameters = database.cursor.statements[0]
    assert "expires_at < to_timestamp(%s)" in cleanup_sql
    assert "now()" not in cleanup_sql
    assert cleanup_parameters == (122.0,)
