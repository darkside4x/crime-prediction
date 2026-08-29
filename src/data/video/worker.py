"""Restart-safe workers for Reka upload, index, analysis and deletion."""

from __future__ import annotations

import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .broker import Delivery, JobBroker, JobMessage
from .coverage import CoverageObservation, CoverageTelemetry
from .errors import VideoPipelineError
from .service import VideoPipelineService

NEXT_OPERATION = {"upload": "index", "index": "analyze"}


@dataclass(frozen=True)
class WorkerResult:
    job_id: str
    state: str
    operation: str
    error_code: str | None = None


class VideoJobWorker:
    """A worker process owns one or more operation types and no tenant secrets."""

    def __init__(
        self,
        *,
        store: Any,
        broker: JobBroker,
        service: VideoPipelineService,
        operations: tuple[str, ...],
        lease_seconds: int = 120,
        worker_id: str | None = None,
        telemetry: CoverageTelemetry | None = None,
    ) -> None:
        allowed = {"upload", "index", "analyze", "delete"}
        if not operations or not set(operations) <= allowed:
            raise ValueError("Worker must own an allowlisted operation")
        self.store = store
        self.broker = broker
        self.service = service
        self.operations = operations
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4()}"
        self.telemetry = telemetry

    def poll_once(self) -> list[WorkerResult]:
        deliveries = self.broker.receive(operations=self.operations, limit=10)
        return [self._handle(delivery) for delivery in deliveries]

    def _handle(self, delivery: Delivery) -> WorkerResult:
        message = delivery.message
        try:
            persisted = self.store.get_job(message.tenant_id, message.job_id)
            if persisted["operation"] != message.operation:
                raise VideoPipelineError("job_message_mismatch", "Queue and persisted job disagree")
            if persisted["state"] == "completed":
                self.broker.acknowledge(delivery)
                return WorkerResult(message.job_id, "completed", message.operation)
            if persisted["state"] in {"failed", "cancelled"}:
                self.broker.acknowledge(delivery)
                return WorkerResult(
                    message.job_id,
                    persisted["state"],
                    message.operation,
                    persisted.get("last_error_code"),
                )
            job = self.store.claim_job(
                message.tenant_id,
                message.job_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            self.broker.heartbeat(delivery, visibility_seconds=self.lease_seconds)
            operation_started = time.monotonic()
            result = self.service.execute_operation(
                message.tenant_id, job["asset_id"], message.operation
            )
            if message.operation == "index" and result in {"pending", "indexing"}:
                delay = _backoff(int(job["attempts"]))
                self.store.transition_job(
                    message.tenant_id,
                    message.job_id,
                    "retry",
                    "reka_index_pending",
                    retry_delay_seconds=delay,
                )
                self.broker.retry(delivery, delay_seconds=delay)
                return WorkerResult(message.job_id, "retry", message.operation, "reka_index_pending")
            self.store.transition_job(message.tenant_id, message.job_id, "completed")
            if message.operation == "analyze":
                self._record_detector(
                    message.tenant_id,
                    job["asset_id"],
                    available=True,
                    latency_ms=int((time.monotonic() - operation_started) * 1000),
                )
            self.broker.acknowledge(delivery)
            next_operation = NEXT_OPERATION.get(message.operation)
            if next_operation:
                next_job = self.store.enqueue(message.tenant_id, job["asset_id"], next_operation)
                if next_job["state"] in {"queued", "retry"}:
                    self.broker.publish(
                        JobMessage(message.tenant_id, next_job["job_id"], next_operation)
                    )
            return WorkerResult(message.job_id, "completed", message.operation)
        except VideoPipelineError as error:
            if error.code in {"job_not_claimable", "job_lease_lost"}:
                self.broker.retry(delivery, delay_seconds=1)
                return WorkerResult(message.job_id, "retry", message.operation, error.code)
            try:
                job = self.store.get_job(message.tenant_id, message.job_id)
            except VideoPipelineError:
                self.broker.dead_letter(delivery, error_code=error.code)
                return WorkerResult(message.job_id, "failed", message.operation, error.code)
            retryable = error.retryable and int(job["attempts"]) < int(job["max_attempts"])
            if error.code.startswith("reka_"):
                self._record_detector(
                    message.tenant_id,
                    job["asset_id"],
                    available=False,
                    reka_available=False,
                )
            if retryable:
                delay = _backoff(int(job["attempts"]))
                self.store.transition_job(
                    message.tenant_id,
                    message.job_id,
                    "retry",
                    error.code,
                    retry_delay_seconds=delay,
                )
                self.broker.retry(delivery, delay_seconds=delay)
                return WorkerResult(message.job_id, "retry", message.operation, error.code)
            self.store.transition_job(message.tenant_id, message.job_id, "failed", error.code)
            self.store.mark_dead_lettered(message.tenant_id, message.job_id)
            self.broker.dead_letter(delivery, error_code=error.code)
            return WorkerResult(message.job_id, "failed", message.operation, error.code)

    def _record_detector(
        self,
        tenant_id: str,
        asset_id: str,
        *,
        available: bool,
        reka_available: bool = True,
        latency_ms: int | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        asset = self.store.get_asset(tenant_id, asset_id)
        start = asset["captured_start"]
        from datetime import datetime

        captured_start = datetime.fromisoformat(start)
        captured_end = datetime.fromisoformat(asset["captured_end"])
        seconds = max(int((captured_end - captured_start).total_seconds()), 1)
        self.telemetry.record(
            CoverageObservation(
                tenant_id=tenant_id,
                source_id=asset["source_id"],
                observed_at=start,
                sample_seconds=seconds,
                connected=True,
                frame_processable=True,
                detector_available=available,
                reka_available=reka_available,
                processing_latency_ms=latency_ms,
            )
        )


def _backoff(attempt: int) -> int:
    return min(2 ** max(attempt - 1, 0), 900)
