"""Durable delivery adapters for isolated video worker stages."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .errors import VideoPipelineError

OPERATIONS = frozenset({"capture", "upload", "index", "analyze", "delete"})


@dataclass(frozen=True)
class JobMessage:
    tenant_id: str
    job_id: str
    operation: str

    def validate(self) -> None:
        try:
            uuid.UUID(self.tenant_id)
            uuid.UUID(self.job_id)
        except (TypeError, ValueError) as error:
            raise VideoPipelineError(
                "job_message_invalid", "Job identifiers must be UUIDs"
            ) from error
        if self.operation not in OPERATIONS:
            raise VideoPipelineError(
                "job_message_invalid", "Job operation is not allowlisted"
            )

    def body(self) -> str:
        self.validate()
        return json.dumps(
            {
                "tenant_id": self.tenant_id,
                "job_id": self.job_id,
                "operation": self.operation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Delivery:
    message: JobMessage
    receipt: str
    receive_count: int
    queue_url: str | None = None


class JobBroker(Protocol):
    def publish(self, message: JobMessage, *, delay_seconds: int = 0) -> None: ...
    def receive(
        self, *, operations: tuple[str, ...], limit: int = 1
    ) -> list[Delivery]: ...
    def acknowledge(self, delivery: Delivery) -> None: ...
    def retry(self, delivery: Delivery, *, delay_seconds: int) -> None: ...
    def heartbeat(self, delivery: Delivery, *, visibility_seconds: int) -> None: ...
    def dead_letter(self, delivery: Delivery, *, error_code: str) -> None: ...
    def depth(self) -> int: ...


class SqsJobBroker:
    """SQS broker with explicit DLQ transfer and bounded message bodies."""

    def __init__(
        self,
        *,
        queue_url: str,
        dead_letter_queue_url: str,
        region_name: str,
        queue_urls: dict[str, str] | None = None,
        dead_letter_queue_urls: dict[str, str] | None = None,
        visibility_seconds: int = 120,
        wait_seconds: int = 10,
        client: object | None = None,
    ) -> None:
        if not queue_url or not dead_letter_queue_url:
            raise ValueError("SQS source and dead-letter queue URLs are required")
        if queue_urls is not None and (
            set(queue_urls) != {"upload", "index", "analyze", "delete"}
            or any(not value for value in queue_urls.values())
        ):
            raise ValueError(
                "Operation queue URLs must define upload, index, analyze, and delete"
            )
        if dead_letter_queue_urls is not None and (
            set(dead_letter_queue_urls) != {"upload", "index", "analyze", "delete"}
            or any(not value for value in dead_letter_queue_urls.values())
        ):
            raise ValueError(
                "Operation dead-letter queue URLs must define upload, index, analyze, and delete"
            )
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover
                raise RuntimeError(
                    "Install the platform extra: pip install -e '.[platform]'"
                ) from error
            client = boto3.client("sqs", region_name=region_name)
        self.client = client
        self.queue_url = queue_url
        self.queue_urls = dict(queue_urls or {})
        self.dead_letter_queue_url = dead_letter_queue_url
        self.dead_letter_queue_urls = dict(dead_letter_queue_urls or {})
        self.visibility_seconds = visibility_seconds
        self.wait_seconds = min(max(wait_seconds, 0), 20)

    def _queue_for(self, operation: str) -> str:
        return self.queue_urls.get(operation, self.queue_url)

    def _dead_letter_queue_for(self, operation: str) -> str:
        return self.dead_letter_queue_urls.get(operation, self.dead_letter_queue_url)

    def publish(self, message: JobMessage, *, delay_seconds: int = 0) -> None:
        kwargs: dict[str, Any] = {
            "QueueUrl": self._queue_for(message.operation),
            "MessageBody": message.body(),
            "DelaySeconds": min(max(int(delay_seconds), 0), 900),
        }
        if kwargs["QueueUrl"].endswith(".fifo"):
            kwargs.update(
                MessageGroupId=f"{message.tenant_id}:{message.operation}",
                MessageDeduplicationId=message.job_id,
            )
        self.client.send_message(**kwargs)

    def receive(self, *, operations: tuple[str, ...], limit: int = 1) -> list[Delivery]:
        allowed = set(operations)
        if not allowed <= OPERATIONS:
            raise ValueError("Worker operation set is invalid")
        deliveries: list[Delivery] = []
        queues: list[tuple[str | None, str]] = (
            [(operation, self._queue_for(operation)) for operation in operations]
            if self.queue_urls
            else [(None, self.queue_url)]
        )
        for expected_operation, queue_url in queues:
            if len(deliveries) >= limit:
                break
            response = self.client.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=min(max(limit - len(deliveries), 1), 10),
                WaitTimeSeconds=self.wait_seconds if len(queues) == 1 else 0,
                VisibilityTimeout=self.visibility_seconds,
                AttributeNames=["ApproximateReceiveCount"],
            )
            for raw in response.get("Messages", []):
                delivery = self._decode(raw, queue_url)
                if delivery is None:
                    continue
                if (
                    expected_operation is not None
                    and delivery.message.operation != expected_operation
                ):
                    self.dead_letter(
                        delivery, error_code="job_queue_operation_mismatch"
                    )
                    continue
                if delivery.message.operation not in allowed:
                    self.retry(delivery, delay_seconds=1)
                    continue
                deliveries.append(delivery)
        return deliveries

    def _decode(self, raw: dict[str, Any], queue_url: str) -> Delivery | None:
        try:
            payload = json.loads(raw["Body"])
            if set(payload) != {"tenant_id", "job_id", "operation"}:
                raise ValueError("unexpected fields")
            message = JobMessage(**payload)
            message.validate()
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            VideoPipelineError,
        ):
            poison = Delivery(
                JobMessage(
                    "00000000-0000-4000-8000-000000000000",
                    "00000000-0000-4000-8000-000000000000",
                    "capture",
                ),
                raw["ReceiptHandle"],
                int(raw.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
                queue_url,
            )
            self.dead_letter(poison, error_code="job_message_invalid")
            return None
        return Delivery(
            message,
            raw["ReceiptHandle"],
            int(raw.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
            queue_url,
        )

    def acknowledge(self, delivery: Delivery) -> None:
        self.client.delete_message(
            QueueUrl=delivery.queue_url or self._queue_for(delivery.message.operation),
            ReceiptHandle=delivery.receipt,
        )

    def retry(self, delivery: Delivery, *, delay_seconds: int) -> None:
        self.client.change_message_visibility(
            QueueUrl=delivery.queue_url or self._queue_for(delivery.message.operation),
            ReceiptHandle=delivery.receipt,
            VisibilityTimeout=min(max(int(delay_seconds), 0), 43200),
        )

    def heartbeat(self, delivery: Delivery, *, visibility_seconds: int) -> None:
        self.client.change_message_visibility(
            QueueUrl=delivery.queue_url or self._queue_for(delivery.message.operation),
            ReceiptHandle=delivery.receipt,
            VisibilityTimeout=min(max(int(visibility_seconds), 1), 43200),
        )

    def dead_letter(self, delivery: Delivery, *, error_code: str) -> None:
        body = json.dumps(
            {
                "tenant_id": delivery.message.tenant_id,
                "job_id": delivery.message.job_id,
                "operation": delivery.message.operation,
                "error_code": error_code,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        queue_url = self._dead_letter_queue_for(delivery.message.operation)
        kwargs: dict[str, Any] = {
            "QueueUrl": queue_url,
            "MessageBody": body,
        }
        if queue_url.endswith(".fifo"):
            kwargs.update(
                MessageGroupId=f"{delivery.message.tenant_id}:{delivery.message.operation}",
                MessageDeduplicationId=f"{delivery.message.job_id}:{error_code}",
            )
        self.client.send_message(**kwargs)
        self.acknowledge(delivery)

    def depth(self) -> int:
        total = 0
        for queue_url in set(self.queue_urls.values()) or {self.queue_url}:
            response = self.client.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
            values = response.get("Attributes", {})
            total += int(values.get("ApproximateNumberOfMessages", 0)) + int(
                values.get("ApproximateNumberOfMessagesNotVisible", 0)
            )
        return total


class DatabaseJobBroker:
    """Durable local broker backed by ``video_processing_jobs`` for development."""

    def __init__(self, store: object) -> None:
        self.store = store

    def publish(self, message: JobMessage, *, delay_seconds: int = 0) -> None:
        message.validate()

    def receive(self, *, operations: tuple[str, ...], limit: int = 1) -> list[Delivery]:
        jobs = self.store.ready_jobs(operations=operations, limit=limit)
        return [
            Delivery(
                JobMessage(job["tenant_id"], job["job_id"], job["operation"]),
                f"database:{job['tenant_id']}:{job['job_id']}",
                int(job["attempts"]) + 1,
            )
            for job in jobs
        ]

    def acknowledge(self, delivery: Delivery) -> None:
        return None

    def retry(self, delivery: Delivery, *, delay_seconds: int) -> None:
        return None

    def heartbeat(self, delivery: Delivery, *, visibility_seconds: int) -> None:
        return None

    def dead_letter(self, delivery: Delivery, *, error_code: str) -> None:
        return None

    def depth(self) -> int:
        return len(self.store.ready_jobs(operations=tuple(OPERATIONS), limit=10000))


class PostgresJobBroker:
    """Restart-safe local broker used by the one-command deployment demo.

    It deliberately stores only opaque tenant/job routing metadata. Tenant-
    specific mutations use transactions with ``SET LOCAL app.tenant_id``;
    cross-tenant claim and depth operations use narrowly scoped database
    functions. AWS deployments use SQS.
    """

    def __init__(self, database: object, *, visibility_seconds: int = 120) -> None:
        self.database = database
        self.visibility_seconds = visibility_seconds

    def publish(self, message: JobMessage, *, delay_seconds: int = 0) -> None:
        message.validate()
        available = datetime.now(UTC) + timedelta(seconds=max(0, delay_seconds))
        with self.database.transaction(message.tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO demo_job_messages
                   (tenant_id,job_id,operation,state,available_at,created_at,updated_at)
                   VALUES (%s,%s,%s,'queued',%s,now(),now())
                   ON CONFLICT (tenant_id,job_id) DO UPDATE SET
                     state=CASE WHEN demo_job_messages.state='dead_letter'
                       THEN demo_job_messages.state ELSE 'queued' END,
                     available_at=excluded.available_at,
                     lease_expires_at=NULL,receipt=NULL,updated_at=now()""",
                (message.tenant_id, message.job_id, message.operation, available),
            )

    def receive(self, *, operations: tuple[str, ...], limit: int = 1) -> list[Delivery]:
        if not operations or not set(operations) <= OPERATIONS - {"capture"}:
            raise ValueError("Worker operation set is invalid")
        claim_limit = min(max(limit, 1), 10)
        visibility_seconds = min(max(int(self.visibility_seconds), 1), 43200)
        with self.database.system_transaction() as cursor:
            cursor.execute(
                """SELECT tenant_id,job_id,operation,receipt,receive_count
                   FROM app.claim_demo_job_messages(%s,%s,%s)""",
                (list(operations), claim_limit, visibility_seconds),
            )
            rows = cursor.fetchall()
        return [
            Delivery(
                JobMessage(str(row["tenant_id"]), str(row["job_id"]), row["operation"]),
                str(row["receipt"]),
                int(row["receive_count"]),
            )
            for row in rows
        ]

    def acknowledge(self, delivery: Delivery) -> None:
        with self.database.transaction(delivery.message.tenant_id) as cursor:
            cursor.execute(
                """DELETE FROM demo_job_messages
                   WHERE tenant_id=%s AND job_id=%s AND receipt=%s""",
                (delivery.message.tenant_id, delivery.message.job_id, delivery.receipt),
            )

    def retry(self, delivery: Delivery, *, delay_seconds: int) -> None:
        available = datetime.now(UTC) + timedelta(seconds=max(0, delay_seconds))
        with self.database.transaction(delivery.message.tenant_id) as cursor:
            cursor.execute(
                """UPDATE demo_job_messages SET state='queued',available_at=%s,
                   lease_expires_at=NULL,receipt=NULL,updated_at=now()
                   WHERE tenant_id=%s AND job_id=%s AND receipt=%s""",
                (available, delivery.message.tenant_id, delivery.message.job_id, delivery.receipt),
            )

    def heartbeat(self, delivery: Delivery, *, visibility_seconds: int) -> None:
        lease = datetime.now(UTC) + timedelta(seconds=max(1, visibility_seconds))
        with self.database.transaction(delivery.message.tenant_id) as cursor:
            cursor.execute(
                """UPDATE demo_job_messages SET lease_expires_at=%s,updated_at=now()
                   WHERE tenant_id=%s AND job_id=%s AND receipt=%s AND state='leased'""",
                (lease, delivery.message.tenant_id, delivery.message.job_id, delivery.receipt),
            )

    def dead_letter(self, delivery: Delivery, *, error_code: str) -> None:
        with self.database.transaction(delivery.message.tenant_id) as cursor:
            cursor.execute(
                """UPDATE demo_job_messages SET state='dead_letter',error_code=%s,
                   lease_expires_at=NULL,receipt=NULL,updated_at=now()
                   WHERE tenant_id=%s AND job_id=%s AND receipt=%s
                     AND state='leased'""",
                (
                    error_code,
                    delivery.message.tenant_id,
                    delivery.message.job_id,
                    delivery.receipt,
                ),
            )

    def depth(self) -> int:
        with self.database.system_transaction() as cursor:
            cursor.execute("SELECT app.demo_job_queue_depth() AS count")
            return int(cursor.fetchone()["count"])
