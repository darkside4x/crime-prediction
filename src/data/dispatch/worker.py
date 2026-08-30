"""Restart-safe SQS worker for bounded dispatch escalation."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from .broker import DispatchBroker, DispatchJob
from .errors import DispatchError, DispatchRetryNotDue, DispatchValidationError
from .service import DispatchCoordinator
from .twilio import MockTwilioVoiceProvider, TwilioMode


class DispatchWorker:
    def __init__(
        self,
        *,
        broker: DispatchBroker,
        coordinator: DispatchCoordinator,
        mode: TwilioMode,
    ) -> None:
        self.broker = broker
        self.coordinator = coordinator
        self.mode = mode

    def poll_once(self, *, wait_seconds: int = 20) -> bool:
        try:
            job = self.broker.receive(wait_seconds=wait_seconds)
        except DispatchValidationError:
            # A malformed message has no trustworthy tenant/case identifiers,
            # so it cannot be acknowledged by the application.  Leaving it
            # untouched lets the queue visibility timeout and redrive policy
            # move it to the DLQ without terminating the worker process.
            return True
        if job is None:
            return False
        try:
            attempt = self.coordinator.dispatch_next(job.tenant_id, job.case_id)
            if attempt is not None and self.mode is TwilioMode.MOCK:
                self._simulate(job, attempt)
            self.broker.acknowledge(job)
            return True
        except DispatchRetryNotDue:
            case = self.coordinator.repository.get_case(job.tenant_id, job.case_id)
            delay = 1
            if case.next_attempt_at is not None:
                delay = max(
                    1,
                    math.ceil(
                        (case.next_attempt_at - datetime.now(UTC)).total_seconds()
                    ),
                )
            self.broker.release(job, delay_seconds=min(delay, 43200))
            return True
        except DispatchError:
            # The queue's redrive policy moves repeatedly failing jobs to the
            # encrypted DLQ. Provider retries reuse the already-reserved
            # logical attempt, so delivery retries cannot exceed three calls.
            if job.receive_count >= 5:
                try:
                    self.coordinator.mark_delivery_exhausted(
                        job.tenant_id,
                        job.case_id,
                    )
                except DispatchError:
                    # The source message must remain unacknowledged even when
                    # the terminal state races another case transition. SQS
                    # redrive remains the durable record of the failure.
                    pass
                return True
            delay = min(300, max(5, 2 ** min(job.receive_count, 8)))
            self.broker.release(job, delay_seconds=delay)
            return True

    def _simulate(self, job: DispatchJob, attempt) -> None:
        reference = attempt.provider_call_reference
        if not reference:
            return
        if reference.startswith("sha256:"):
            provider = self.coordinator.voice_provider
            if not isinstance(provider, MockTwilioVoiceProvider):
                # Only the deterministic mock provider may reconstruct an
                # addressable reference. A durable live SID remains one-way.
                return
            reference = provider.reference_for_request(attempt.attempt_id)
        if attempt.sequence < 3:
            case = self.coordinator.handle_status(
                attempt.callback_token,
                provider_call_reference=reference,
                status="no_answer",
                event_key=f"mock-no-answer-{attempt.sequence}",
            )
            if case.next_attempt_at is not None:
                delay = max(
                    0,
                    math.ceil(
                        (case.next_attempt_at - datetime.now(UTC)).total_seconds()
                    ),
                )
                self.broker.enqueue(
                    job.tenant_id, job.case_id, delay_seconds=min(delay, 900)
                )
        else:
            self.coordinator.handle_gather(
                attempt.callback_token,
                provider_call_reference=reference,
                digits="1",
                event_key="mock-supervisor-acknowledged",
            )


__all__ = ["DispatchWorker"]
