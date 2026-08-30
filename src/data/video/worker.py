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
        index_max_attempts: int = 20,
        index_poll_seconds: int = 3,
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
        if not 1 <= index_max_attempts <= 100:
            raise ValueError("index_max_attempts must be between 1 and 100")
        if not 0 <= index_poll_seconds <= 30:
            raise ValueError("index_poll_seconds must be between 0 and 30")
        self.index_max_attempts = index_max_attempts
        self.index_poll_seconds = index_poll_seconds

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
            if (
                message.operation == "index"
                and persisted["state"] == "retry"
                and persisted.get("last_error_code") == "reka_index_pending"
                and int(persisted["attempts"]) >= int(persisted["max_attempts"])
                and int(persisted["max_attempts"]) < self.index_max_attempts
            ):
                persisted = self.store.extend_index_attempt_limit(
                    message.tenant_id,
                    message.job_id,
                    max_attempts=self.index_max_attempts,
                )
            if (
                persisted["state"] == "retry"
                and int(persisted["attempts"]) >= int(persisted["max_attempts"])
            ):
                error_code = (
                    "reka_index_timeout"
                    if message.operation == "index"
                    and persisted.get("last_error_code") == "reka_index_pending"
                    else "job_attempts_exhausted"
                )
                self.store.transition_job(
                    message.tenant_id, message.job_id, "failed", error_code
                )
                if message.operation in {"upload", "index", "analyze"}:
                    self.store.update_asset_status(
                        message.tenant_id,
                        persisted["asset_id"],
                        "failed",
                        error_code,
                    )
                self.store.mark_dead_lettered(message.tenant_id, message.job_id)
                self.store.audit(
                    message.tenant_id,
                    f"reka.{message.operation}",
                    "asset",
                    persisted["asset_id"],
                    "failure",
                    error_code,
                )
                if message.operation == "index":
                    self._record_detector(
                        message.tenant_id,
                        persisted["asset_id"],
                        available=False,
                        reka_available=True,
                        degraded_reason="reka_index_timeout",
                    )
                self.broker.dead_letter(delivery, error_code=error_code)
                return WorkerResult(
                    message.job_id, "failed", message.operation, error_code
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
                if int(job["attempts"]) >= int(job["max_attempts"]):
                    error_code = "reka_index_timeout"
                    self.store.transition_job(
                        message.tenant_id, message.job_id, "failed", error_code
                    )
                    self.store.update_asset_status(
                        message.tenant_id,
                        job["asset_id"],
                        "failed",
                        error_code,
                    )
                    self.store.mark_dead_lettered(message.tenant_id, message.job_id)
                    self.store.audit(
                        message.tenant_id,
                        "reka.index",
                        "asset",
                        job["asset_id"],
                        "failure",
                        error_code,
                    )
                    self._record_detector(
                        message.tenant_id,
                        job["asset_id"],
                        available=False,
                        reka_available=True,
                        degraded_reason="reka_index_timeout",
                    )
                    self.broker.dead_letter(delivery, error_code=error_code)
                    return WorkerResult(
                        message.job_id, "failed", message.operation, error_code
                    )
                delay = self.index_poll_seconds
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
            self.store.audit(
                message.tenant_id,
                f"reka.{message.operation}",
                "asset",
                job["asset_id"],
                "success",
            )
            self.broker.acknowledge(delivery)
            next_operation = NEXT_OPERATION.get(message.operation)
            if next_operation:
                next_job = self.store.enqueue(
                    message.tenant_id,
                    job["asset_id"],
                    next_operation,
                    max_attempts=(
                        self.index_max_attempts
                        if next_operation == "index"
                        else 5
                    ),
                )
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
            degradation = (
                _reka_degradation(error.code)
                if error.code.startswith("reka_")
                else None
            )
            if retryable:
                delay = _backoff(int(job["attempts"]))
                self.store.transition_job(
                    message.tenant_id,
                    message.job_id,
                    "retry",
                    error.code,
                    retry_delay_seconds=delay,
                    safe_diagnostics=error.safe_diagnostics,
                )
                self.store.audit(
                    message.tenant_id,
                    f"reka.{message.operation}",
                    "asset",
                    job["asset_id"],
                    "failure",
                    error.code,
                )
                if degradation is not None:
                    degraded_reason, reka_available = degradation
                    self._record_detector(
                        message.tenant_id,
                        job["asset_id"],
                        available=False,
                        reka_available=reka_available,
                        degraded_reason=degraded_reason,
                    )
                self.broker.retry(delivery, delay_seconds=delay)
                return WorkerResult(message.job_id, "retry", message.operation, error.code)
            self.store.transition_job(
                message.tenant_id,
                message.job_id,
                "failed",
                error.code,
                safe_diagnostics=error.safe_diagnostics,
            )
            if message.operation in {"upload", "index", "analyze"}:
                self.store.update_asset_status(
                    message.tenant_id, job["asset_id"], "failed", error.code
                )
            self.store.mark_dead_lettered(message.tenant_id, message.job_id)
            self.store.audit(
                message.tenant_id,
                f"reka.{message.operation}",
                "asset",
                job["asset_id"],
                "failure",
                error.code,
            )
            if degradation is not None:
                degraded_reason, reka_available = degradation
                self._record_detector(
                    message.tenant_id,
                    job["asset_id"],
                    available=False,
                    reka_available=reka_available,
                    degraded_reason=degraded_reason,
                )
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
        degraded_reason: str = "detector_gap",
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
        # Persist the bounded clip window immediately so the demo scheduler and
        # forecast API consume measured coverage rather than a seeded value.
        self.service.record_coverage(
            tenant_id=tenant_id,
            source_id=asset["source_id"],
            interval_start=asset["captured_start"],
            interval_end=asset["captured_end"],
            connected_seconds=seconds,
            processable_seconds=seconds,
            detector_available_seconds=seconds if available else 0,
            degraded_reason_codes=[] if available else [degraded_reason],
        )


def _backoff(attempt: int) -> int:
    return min(2 ** max(attempt - 1, 0), 900)


def _reka_degradation(error_code: str) -> tuple[str, bool]:
    if error_code.startswith("reka_output_") or error_code == "reka_response_invalid":
        return "detector_output_invalid", True
    if error_code in {"reka_index_failed", "reka_request_failed", "reka_upload_failed"}:
        return "reka_request_rejected", True
    if error_code in {"reka_access_denied", "reka_key_missing"}:
        return "reka_configuration_error", False
    return "reka_unavailable", False
