from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from src.data.video.broker import Delivery, JobMessage, PostgresJobBroker

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
JOB_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
JOB_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class RecordingCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []

    def execute(self, statement: str, values: tuple[Any, ...] | None = None) -> None:
        self.statements.append((statement, values))

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def fetchone(self) -> dict[str, Any]:
        return self.rows[0]


class RecordingDatabase:
    def __init__(self) -> None:
        self.tenant_calls: list[tuple[str, RecordingCursor]] = []
        self.system_calls: list[RecordingCursor] = []
        self.next_system_rows: list[dict[str, Any]] = []

    @contextmanager
    def transaction(self, tenant_id: str):
        cursor = RecordingCursor()
        self.tenant_calls.append((tenant_id, cursor))
        yield cursor

    @contextmanager
    def system_transaction(self):
        cursor = RecordingCursor(self.next_system_rows)
        self.system_calls.append(cursor)
        yield cursor


def test_postgres_broker_mutations_always_set_the_delivery_tenant() -> None:
    database = RecordingDatabase()
    broker = PostgresJobBroker(database)
    message = JobMessage(TENANT_A, JOB_A, "upload")
    delivery = Delivery(message, "cccccccc-cccc-4ccc-8ccc-cccccccccccc", 1)

    broker.publish(message)
    broker.acknowledge(delivery)
    broker.retry(delivery, delay_seconds=2)
    broker.heartbeat(delivery, visibility_seconds=3)
    broker.dead_letter(delivery, error_code="reka_timeout")

    assert [tenant_id for tenant_id, _ in database.tenant_calls] == [TENANT_A] * 5
    assert database.system_calls == []
    dead_letter_sql, dead_letter_values = database.tenant_calls[-1][1].statements[0]
    assert "receipt=%s" in dead_letter_sql
    assert "state='leased'" in dead_letter_sql
    assert dead_letter_values == (
        "reka_timeout",
        TENANT_A,
        JOB_A,
        delivery.receipt,
    )


def test_postgres_broker_uses_only_scoped_functions_for_cross_tenant_work() -> None:
    database = RecordingDatabase()
    database.next_system_rows = [
        {
            "tenant_id": TENANT_A,
            "job_id": JOB_A,
            "operation": "upload",
            "receipt": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "receive_count": 2,
        },
        {
            "tenant_id": TENANT_B,
            "job_id": JOB_B,
            "operation": "upload",
            "receipt": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "receive_count": 1,
        },
    ]
    broker = PostgresJobBroker(database, visibility_seconds=120)

    deliveries = broker.receive(operations=("upload",), limit=2)

    assert [delivery.message.tenant_id for delivery in deliveries] == [
        TENANT_A,
        TENANT_B,
    ]
    claim_sql, claim_values = database.system_calls[0].statements[0]
    assert "app.claim_demo_job_messages" in claim_sql
    assert "UPDATE demo_job_messages" not in claim_sql
    assert claim_values == (["upload"], 2, 120)

    database.next_system_rows = [{"count": 2}]
    assert broker.depth() == 2
    depth_sql, depth_values = database.system_calls[1].statements[0]
    assert depth_sql == "SELECT app.demo_job_queue_depth() AS count"
    assert depth_values is None


def test_demo_queue_migration_forces_rls_and_locks_down_definer_functions() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/postgres/011_demo_job_queue_rls.sql").read_text(
        encoding="utf-8"
    )

    assert "ALTER TABLE public.demo_job_messages FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_id = app.current_tenant_id()" in migration
    assert "SECURITY DEFINER" in migration
    assert migration.count("SET search_path = pg_catalog, public") == 2
    assert "FROM PUBLIC" in migration
    assert migration.count("TO crime_app") == 2
