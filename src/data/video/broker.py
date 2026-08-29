"""Durable delivery adapters for isolated video worker stages."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
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
            raise VideoPipelineError("job_message_invalid", "Job identifiers must be UUIDs") from error
        if self.operation not in OPERATIONS:
            raise VideoPipelineError("job_message_invalid", "Job operation is not allowlisted")

    def body(self) -> str:
        self.validate()
        return json.dumps(
            {"tenant_id": self.tenant_id, "job_id": self.job_id, "operation": self.operation},
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Delivery:
    message: JobMessage
    receipt: str
    receive_count: int


class JobBroker(Protocol):
    def publish(self, message: JobMessage, *, delay_seconds: int = 0) -> None: ...
    def receive(self, *, operations: tuple[str, ...], limit: int = 1) -> list[Delivery]: ...
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
        visibility_seconds: int = 120,
        wait_seconds: int = 10,
        client: object | None = None,
    ) -> None:
        if not queue_url or not dead_letter_queue_url:
            raise ValueError("SQS source and dead-letter queue URLs are required")
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover
                raise RuntimeError("Install the platform extra: pip install -e '.[platform]'") from error
            client = boto3.client("sqs", region_name=region_name)
        self.client = client
        self.queue_url = queue_url
        self.dead_letter_queue_url = dead_letter_queue_url
        self.visibility_seconds = visibility_seconds
        self.wait_seconds = min(max(wait_seconds, 0), 20)

    def publish(self, message: JobMessage, *, delay_seconds: int = 0) -> None:
        kwargs: dict[str, Any] = {
            "QueueUrl": self.queue_url,
            "MessageBody": message.body(),
            "DelaySeconds": min(max(int(delay_seconds), 0), 900),
        }
        if self.queue_url.endswith(".fifo"):
            kwargs.update(
                MessageGroupId=f"{message.tenant_id}:{message.operation}",
                MessageDeduplicationId=message.job_id,
            )
        self.client.send_message(**kwargs)

    def receive(self, *, operations: tuple[str, ...], limit: int = 1) -> list[Delivery]:
        allowed = set(operations)
        if not allowed <= OPERATIONS:
            raise ValueError("Worker operation set is invalid")
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=min(max(limit, 1), 10),
            WaitTimeSeconds=self.wait_seconds,
            VisibilityTimeout=self.visibility_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        deliveries: list[Delivery] = []
        for raw in response.get("Messages", []):
            try:
                payload = json.loads(raw["Body"])
                if set(payload) != {"tenant_id", "job_id", "operation"}:
                    raise ValueError("unexpected fields")
                message = JobMessage(**payload)
                message.validate()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, VideoPipelineError):
                poison = Delivery(
                    JobMessage(
                        "00000000-0000-4000-8000-000000000000",
                        "00000000-0000-4000-8000-000000000000",
                        "capture",
                    ),
                    raw["ReceiptHandle"],
                    int(raw.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
                )
                self.dead_letter(poison, error_code="job_message_invalid")
                continue
            delivery = Delivery(
                message,
                raw["ReceiptHandle"],
                int(raw.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
            )
            if message.operation not in allowed:
                self.retry(delivery, delay_seconds=1)
                continue
            deliveries.append(delivery)
        return deliveries

    def acknowledge(self, delivery: Delivery) -> None:
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=delivery.receipt)

    def retry(self, delivery: Delivery, *, delay_seconds: int) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=delivery.receipt,
            VisibilityTimeout=min(max(int(delay_seconds), 0), 43200),
        )

    def heartbeat(self, delivery: Delivery, *, visibility_seconds: int) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url,
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
        kwargs: dict[str, Any] = {"QueueUrl": self.dead_letter_queue_url, "MessageBody": body}
        if self.dead_letter_queue_url.endswith(".fifo"):
            kwargs.update(
                MessageGroupId=f"{delivery.message.tenant_id}:{delivery.message.operation}",
                MessageDeduplicationId=f"{delivery.message.job_id}:{error_code}",
            )
        self.client.send_message(**kwargs)
        self.acknowledge(delivery)

    def depth(self) -> int:
        response = self.client.get_queue_attributes(
            QueueUrl=self.queue_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )
        values = response.get("Attributes", {})
        return int(values.get("ApproximateNumberOfMessages", 0)) + int(
            values.get("ApproximateNumberOfMessagesNotVisible", 0)
        )


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
