"""Durable SQS transport for human-authorized dispatch cases."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import DispatchConfigurationError, DispatchValidationError


@dataclass(frozen=True)
class DispatchJob:
    tenant_id: str
    case_id: str
    receipt_handle: str | None = None
    receive_count: int = 1

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.tenant_id)
            uuid.UUID(self.case_id)
        except (TypeError, ValueError) as error:
            raise DispatchValidationError(
                "dispatch_job_invalid", "Dispatch job identifiers are invalid"
            ) from error
        if self.receive_count < 1:
            raise DispatchValidationError(
                "dispatch_job_invalid", "Dispatch job receive count is invalid"
            )


class DispatchBroker(Protocol):
    def enqueue(
        self, tenant_id: str, case_id: str, *, delay_seconds: int = 0
    ) -> None: ...
    def receive(self, *, wait_seconds: int = 20) -> DispatchJob | None: ...
    def acknowledge(self, job: DispatchJob) -> None: ...
    def release(self, job: DispatchJob, *, delay_seconds: int) -> None: ...


class InMemoryDispatchBroker:
    development_only = True

    def __init__(self) -> None:
        self.jobs: list[DispatchJob] = []

    def enqueue(self, tenant_id: str, case_id: str, *, delay_seconds: int = 0) -> None:
        del delay_seconds
        candidate = DispatchJob(tenant_id=tenant_id, case_id=case_id)
        if not any(
            item.tenant_id == tenant_id and item.case_id == case_id
            for item in self.jobs
        ):
            self.jobs.append(candidate)

    def receive(self, *, wait_seconds: int = 20) -> DispatchJob | None:
        del wait_seconds
        return self.jobs[0] if self.jobs else None

    def acknowledge(self, job: DispatchJob) -> None:
        if self.jobs and self.jobs[0] == job:
            self.jobs.pop(0)

    def release(self, job: DispatchJob, *, delay_seconds: int) -> None:
        del job, delay_seconds


class SqsDispatchBroker:
    """Small strict SQS adapter; queue redrive owns poison-message handling."""

    development_only = False

    def __init__(
        self,
        queue_url: str,
        *,
        region_name: str,
        client: Any | None = None,
    ) -> None:
        if not queue_url.startswith("https://sqs."):
            raise DispatchConfigurationError("dispatch_queue_invalid")
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover
                raise DispatchConfigurationError("aws_sdk_unavailable") from error
            client = boto3.client("sqs", region_name=region_name)
        self.queue_url = queue_url
        self.client = client

    @staticmethod
    def _body(tenant_id: str, case_id: str) -> str:
        DispatchJob(tenant_id=tenant_id, case_id=case_id)
        return json.dumps(
            {
                "schema_version": "1.0.0",
                "tenant_id": tenant_id,
                "dispatch_case_id": case_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def enqueue(self, tenant_id: str, case_id: str, *, delay_seconds: int = 0) -> None:
        if not 0 <= delay_seconds <= 900:
            raise DispatchValidationError(
                "dispatch_delay_invalid", "Dispatch queue delay is invalid"
            )
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=self._body(tenant_id, case_id),
            DelaySeconds=delay_seconds,
        )

    def receive(self, *, wait_seconds: int = 20) -> DispatchJob | None:
        if not 0 <= wait_seconds <= 20:
            raise ValueError("wait_seconds must be between 0 and 20")
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages", [])
        if not messages:
            return None
        message = messages[0]
        try:
            body = json.loads(message["Body"])
            if not isinstance(body, dict):
                raise TypeError("message body must be an object")
            if body.get("schema_version") != "1.0.0":
                raise ValueError("unsupported schema")
            receipt_handle = message["ReceiptHandle"]
            attributes = message.get("Attributes", {})
            if not isinstance(receipt_handle, str) or not receipt_handle:
                raise ValueError("missing receipt")
            if not isinstance(attributes, dict):
                raise TypeError("invalid attributes")
            return DispatchJob(
                tenant_id=str(body["tenant_id"]),
                case_id=str(body["dispatch_case_id"]),
                receipt_handle=receipt_handle,
                receive_count=int(
                    attributes.get("ApproximateReceiveCount", "1")
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            # Do not acknowledge malformed jobs. SQS redrive moves them to the
            # encrypted DLQ after the configured receive limit.
            raise DispatchValidationError(
                "dispatch_job_invalid", "Dispatch queue message is invalid"
            ) from error

    def acknowledge(self, job: DispatchJob) -> None:
        if not job.receipt_handle:
            raise DispatchValidationError(
                "dispatch_job_invalid", "Dispatch receipt is unavailable"
            )
        self.client.delete_message(
            QueueUrl=self.queue_url, ReceiptHandle=job.receipt_handle
        )

    def release(self, job: DispatchJob, *, delay_seconds: int) -> None:
        if not job.receipt_handle or not 0 <= delay_seconds <= 43200:
            raise DispatchValidationError(
                "dispatch_delay_invalid", "Dispatch retry delay is invalid"
            )
        self.client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=job.receipt_handle,
            VisibilityTimeout=delay_seconds,
        )


__all__ = [
    "DispatchBroker",
    "DispatchJob",
    "InMemoryDispatchBroker",
    "SqsDispatchBroker",
]
