"""Persistence interface and offline implementation for dispatch state."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Literal, Protocol

from .errors import (
    DispatchResourceNotFound,
    DispatchRetryNotDue,
    DispatchStateConflict,
    WebhookCallMismatch,
)
from .models import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_DISPATCH_STATUSES,
    CallAttempt,
    CallAttemptStatus,
    ContactRole,
    DispatchCase,
    DispatchEvent,
    DispatchEventKind,
    DispatchStatus,
    require_utc,
)


@dataclass(frozen=True)
class AttemptReservation:
    case: DispatchCase
    attempt: CallAttempt
    created: bool


@dataclass(frozen=True)
class ProviderSubmissionClaim:
    """Result of atomically claiming the one external call side effect."""

    case: DispatchCase
    attempt: CallAttempt
    acquired: bool
    stale: bool = False
    retry_at: datetime | None = None


@dataclass(frozen=True)
class _ProviderSubmission:
    state: Literal["pending", "claimed", "submitted", "uncertain"] = "pending"
    owner_hash: str | None = None
    claimed_at: datetime | None = None
    deadline: datetime | None = None
    completed_at: datetime | None = None


def _claim_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DispatchRepository(Protocol):
    """Atomic operations required from a durable dispatch repository."""

    def create_case(
        self,
        case: DispatchCase,
        *,
        idempotency_key: str,
        event: DispatchEvent,
    ) -> tuple[DispatchCase, bool]: ...

    def find_case_by_idempotency(
        self, tenant_id: str, idempotency_key: str
    ) -> DispatchCase | None: ...

    def get_case(self, tenant_id: str, case_id: str) -> DispatchCase: ...

    def list_attempts(
        self, tenant_id: str, case_id: str
    ) -> tuple[CallAttempt, ...]: ...

    def reserve_next_attempt(
        self,
        tenant_id: str,
        case_id: str,
        *,
        attempt_id: str,
        callback_token: str,
        at: datetime,
        event: DispatchEvent,
    ) -> AttemptReservation: ...

    def claim_provider_submission(
        self,
        tenant_id: str,
        case_id: str,
        attempt_id: str,
        *,
        claim_token: str,
        at: datetime,
        deadline: datetime,
    ) -> ProviderSubmissionClaim: ...

    def finish_provider_submission(
        self,
        *,
        case: DispatchCase,
        attempt: CallAttempt,
        event: DispatchEvent,
        claim_token: str,
        outcome: Literal["submitted", "retryable_failure", "uncertain"],
        expected_case_version: int,
        expected_attempt_version: int,
    ) -> bool: ...

    def bind_provider_callback(
        self,
        callback_token: str,
        provider_call_reference: str,
        *,
        at: datetime,
        event: DispatchEvent,
    ) -> tuple[DispatchCase, CallAttempt]: ...

    def expire_provider_submission(
        self,
        *,
        case: DispatchCase,
        attempt: CallAttempt,
        event: DispatchEvent,
        expired_at: datetime,
        expected_case_version: int,
        expected_attempt_version: int,
    ) -> bool: ...

    def apply_transition(
        self,
        *,
        case: DispatchCase,
        attempt: CallAttempt,
        event: DispatchEvent,
        expected_case_version: int,
        expected_attempt_version: int,
    ) -> bool: ...

    def apply_case_transition(
        self,
        *,
        case: DispatchCase,
        event: DispatchEvent,
        expected_case_version: int,
    ) -> bool: ...

    def append_event(self, event: DispatchEvent) -> bool: ...

    def resolve_callback(
        self, callback_token: str
    ) -> tuple[DispatchCase, CallAttempt]: ...

    def list_events(
        self, tenant_id: str, case_id: str
    ) -> tuple[DispatchEvent, ...]: ...


class InMemoryDispatchRepository:
    """Thread-safe reference implementation preserving repository invariants."""

    def __init__(self) -> None:
        self._cases: dict[tuple[str, str], DispatchCase] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._attempts: dict[tuple[str, str], CallAttempt] = {}
        self._attempt_ids_by_case: dict[tuple[str, str], list[str]] = {}
        self._callback_index: dict[str, tuple[str, str]] = {}
        self._provider_submissions: dict[
            tuple[str, str], _ProviderSubmission
        ] = {}
        self._events: dict[tuple[str, str], list[DispatchEvent]] = {}
        self._event_dedupe: set[tuple[str, str]] = set()
        self._lock = RLock()

    def create_case(
        self,
        case: DispatchCase,
        *,
        idempotency_key: str,
        event: DispatchEvent,
    ) -> tuple[DispatchCase, bool]:
        with self._lock:
            idempotency_scope = (case.tenant_id, idempotency_key)
            existing_id = self._idempotency.get(idempotency_scope)
            if existing_id is not None:
                return self._cases[(case.tenant_id, existing_id)], False
            case_key = (case.tenant_id, case.case_id)
            if case_key in self._cases:
                raise DispatchStateConflict("dispatch_repository_conflict")
            self._validate_event(event, case=case, attempt=None)
            self._cases[case_key] = case
            self._idempotency[idempotency_scope] = case.case_id
            self._attempt_ids_by_case[case_key] = []
            self._events[case_key] = [event]
            self._event_dedupe.add((case.tenant_id, event.deduplication_key))
            return case, True

    def find_case_by_idempotency(
        self, tenant_id: str, idempotency_key: str
    ) -> DispatchCase | None:
        with self._lock:
            case_id = self._idempotency.get((tenant_id, idempotency_key))
            return None if case_id is None else self._cases[(tenant_id, case_id)]

    def get_case(self, tenant_id: str, case_id: str) -> DispatchCase:
        with self._lock:
            case = self._cases.get((tenant_id, case_id))
        if case is None:
            raise DispatchResourceNotFound()
        return case

    def list_attempts(self, tenant_id: str, case_id: str) -> tuple[CallAttempt, ...]:
        with self._lock:
            key = (tenant_id, case_id)
            if key not in self._cases:
                raise DispatchResourceNotFound()
            return tuple(
                self._attempts[(tenant_id, attempt_id)]
                for attempt_id in self._attempt_ids_by_case[key]
            )

    def reserve_next_attempt(
        self,
        tenant_id: str,
        case_id: str,
        *,
        attempt_id: str,
        callback_token: str,
        at: datetime,
        event: DispatchEvent,
    ) -> AttemptReservation:
        now = require_utc(at, "attempt reservation time")
        with self._lock:
            case_key = (tenant_id, case_id)
            case = self._cases.get(case_key)
            if case is None:
                raise DispatchResourceNotFound()
            attempts = [
                self._attempts[(tenant_id, item)]
                for item in self._attempt_ids_by_case[case_key]
            ]
            active = [
                attempt
                for attempt in attempts
                if attempt.status not in TERMINAL_ATTEMPT_STATUSES
            ]
            if active:
                return AttemptReservation(case=case, attempt=active[-1], created=False)
            if case.status in TERMINAL_DISPATCH_STATUSES:
                raise DispatchStateConflict("dispatch_case_terminal")
            if case.next_attempt_at is not None and now < case.next_attempt_at:
                raise DispatchRetryNotDue()

            sequence = len(attempts) + 1
            if sequence > 3:
                raise DispatchStateConflict("dispatch_attempt_limit_reached")
            role = ContactRole.PRIMARY if sequence <= 2 else ContactRole.SUPERVISOR
            contact_id = (
                case.primary_contact_id
                if role is ContactRole.PRIMARY
                else case.supervisor_contact_id
            )
            attempt = CallAttempt(
                attempt_id=attempt_id,
                case_id=case.case_id,
                tenant_id=case.tenant_id,
                sequence=sequence,
                target_role=role,
                contact_id=contact_id,
                callback_token=callback_token,
                provider_call_reference=None,
                status=CallAttemptStatus.RESERVED,
                created_at=now,
                updated_at=now,
            )
            if (
                tenant_id,
                attempt_id,
            ) in self._attempts or callback_token in self._callback_index:
                raise DispatchStateConflict("dispatch_repository_conflict")
            updated_case = replace(
                case,
                status=DispatchStatus.DIALING,
                next_attempt_at=None,
                attempt_count=sequence,
                updated_at=now,
                version=case.version + 1,
            )
            self._validate_event(event, case=updated_case, attempt=attempt)
            self._cases[case_key] = updated_case
            self._attempts[(tenant_id, attempt_id)] = attempt
            self._provider_submissions[(tenant_id, attempt_id)] = _ProviderSubmission()
            self._attempt_ids_by_case[case_key].append(attempt_id)
            self._callback_index[callback_token] = (tenant_id, attempt_id)
            self._append_event_locked(event)
            return AttemptReservation(case=updated_case, attempt=attempt, created=True)

    def claim_provider_submission(
        self,
        tenant_id: str,
        case_id: str,
        attempt_id: str,
        *,
        claim_token: str,
        at: datetime,
        deadline: datetime,
    ) -> ProviderSubmissionClaim:
        now = require_utc(at, "provider submission claim time")
        expires = require_utc(deadline, "provider submission claim deadline")
        if expires <= now or not claim_token:
            raise DispatchStateConflict("dispatch_submission_claim_invalid")
        with self._lock:
            case = self._cases.get((tenant_id, case_id))
            attempt = self._attempts.get((tenant_id, attempt_id))
            submission = self._provider_submissions.get((tenant_id, attempt_id))
            if case is None or attempt is None or submission is None:
                raise DispatchResourceNotFound()
            if attempt.case_id != case_id:
                raise DispatchStateConflict()
            if case.status in TERMINAL_DISPATCH_STATUSES:
                return ProviderSubmissionClaim(case=case, attempt=attempt, acquired=False)
            if submission.state == "claimed":
                if submission.deadline is not None and submission.deadline <= now:
                    return ProviderSubmissionClaim(
                        case=case,
                        attempt=attempt,
                        acquired=False,
                        stale=True,
                        retry_at=submission.deadline,
                    )
                return ProviderSubmissionClaim(
                    case=case,
                    attempt=attempt,
                    acquired=False,
                    retry_at=submission.deadline,
                )
            if submission.state in {"submitted", "uncertain"}:
                return ProviderSubmissionClaim(case=case, attempt=attempt, acquired=False)
            if case.next_attempt_at is not None and now < case.next_attempt_at:
                raise DispatchRetryNotDue()
            if attempt.status not in {
                CallAttemptStatus.RESERVED,
                CallAttemptStatus.PROVIDER_RETRY,
            }:
                return ProviderSubmissionClaim(case=case, attempt=attempt, acquired=False)
            claimed_case = replace(
                case,
                next_attempt_at=expires,
                updated_at=now,
                version=case.version + 1,
            )
            claimed_attempt = replace(
                attempt,
                updated_at=now,
                version=attempt.version + 1,
            )
            self._cases[(tenant_id, case_id)] = claimed_case
            self._attempts[(tenant_id, attempt_id)] = claimed_attempt
            self._provider_submissions[(tenant_id, attempt_id)] = _ProviderSubmission(
                state="claimed",
                owner_hash=_claim_digest(claim_token),
                claimed_at=now,
                deadline=expires,
            )
            return ProviderSubmissionClaim(
                case=claimed_case,
                attempt=claimed_attempt,
                acquired=True,
                retry_at=expires,
            )

    def finish_provider_submission(
        self,
        *,
        case: DispatchCase,
        attempt: CallAttempt,
        event: DispatchEvent,
        claim_token: str,
        outcome: Literal["submitted", "retryable_failure", "uncertain"],
        expected_case_version: int,
        expected_attempt_version: int,
    ) -> bool:
        with self._lock:
            case_key = (case.tenant_id, case.case_id)
            attempt_key = (attempt.tenant_id, attempt.attempt_id)
            current_case = self._cases.get(case_key)
            current_attempt = self._attempts.get(attempt_key)
            submission = self._provider_submissions.get(attempt_key)
            if current_case is None or current_attempt is None or submission is None:
                raise DispatchResourceNotFound()
            if (case.tenant_id, event.deduplication_key) in self._event_dedupe:
                return False
            if (
                submission.state != "claimed"
                or submission.owner_hash is None
                or not hmac.compare_digest(
                    submission.owner_hash, _claim_digest(claim_token)
                )
                or current_case.version != expected_case_version
                or current_attempt.version != expected_attempt_version
                or case.version != expected_case_version + 1
                or attempt.version != expected_attempt_version + 1
            ):
                raise DispatchStateConflict("dispatch_repository_conflict")
            self._validate_submission_outcome(outcome, case, attempt)
            self._validate_event(event, case=case, attempt=attempt)
            if outcome == "retryable_failure":
                next_submission = _ProviderSubmission()
            else:
                next_submission = replace(
                    submission,
                    state="submitted" if outcome == "submitted" else "uncertain",
                    completed_at=attempt.updated_at,
                )
            self._cases[case_key] = case
            self._attempts[attempt_key] = attempt
            self._provider_submissions[attempt_key] = next_submission
            self._append_event_locked(event)
            return True

    def bind_provider_callback(
        self,
        callback_token: str,
        provider_call_reference: str,
        *,
        at: datetime,
        event: DispatchEvent,
    ) -> tuple[DispatchCase, CallAttempt]:
        """Bind the first provider-authenticated callback to a claimed call.

        Twilio can request the voice URL before ``calls.create`` returns to the
        submitting worker.  The callback is therefore allowed to complete the
        durable provider-submission claim.  A different reference can never
        replace the first one.
        """

        now = require_utc(at, "provider callback binding time")
        if not provider_call_reference:
            raise WebhookCallMismatch()
        with self._lock:
            mapping = self._callback_index.get(callback_token)
            if mapping is None:
                raise DispatchResourceNotFound()
            tenant_id, attempt_id = mapping
            attempt_key = (tenant_id, attempt_id)
            current_attempt = self._attempts[attempt_key]
            case_key = (tenant_id, current_attempt.case_id)
            current_case = self._cases[case_key]
            submission = self._provider_submissions[attempt_key]

            stored_reference = current_attempt.provider_call_reference
            if stored_reference is not None:
                if not self._provider_reference_matches(
                    stored_reference, provider_call_reference
                ):
                    raise WebhookCallMismatch()
                return current_case, current_attempt
            if (
                current_case.status in TERMINAL_DISPATCH_STATUSES
                or submission.state != "claimed"
            ):
                raise DispatchStateConflict("dispatch_callback_binding_unavailable")
            if (tenant_id, event.deduplication_key) in self._event_dedupe:
                raise DispatchStateConflict("dispatch_repository_conflict")
            completed_at = max(now, submission.claimed_at or now)

            updated_attempt = replace(
                current_attempt,
                provider_call_reference=provider_call_reference,
                status=CallAttemptStatus.INITIATED,
                initiated_at=current_attempt.initiated_at or completed_at,
                safe_error_code=None,
                updated_at=completed_at,
                version=current_attempt.version + 1,
            )
            updated_case = replace(
                current_case,
                status=DispatchStatus.DIALING,
                next_attempt_at=None,
                updated_at=completed_at,
                version=current_case.version + 1,
            )
            self._validate_submission_outcome(
                "submitted", updated_case, updated_attempt
            )
            self._validate_event(
                event, case=updated_case, attempt=updated_attempt
            )
            self._cases[case_key] = updated_case
            self._attempts[attempt_key] = updated_attempt
            self._provider_submissions[attempt_key] = replace(
                submission, state="submitted", completed_at=completed_at
            )
            self._append_event_locked(event)
            return updated_case, updated_attempt

    def expire_provider_submission(
        self,
        *,
        case: DispatchCase,
        attempt: CallAttempt,
        event: DispatchEvent,
        expired_at: datetime,
        expected_case_version: int,
        expected_attempt_version: int,
    ) -> bool:
        now = require_utc(expired_at, "provider submission expiry time")
        with self._lock:
            case_key = (case.tenant_id, case.case_id)
            attempt_key = (attempt.tenant_id, attempt.attempt_id)
            current_case = self._cases.get(case_key)
            current_attempt = self._attempts.get(attempt_key)
            submission = self._provider_submissions.get(attempt_key)
            if current_case is None or current_attempt is None or submission is None:
                raise DispatchResourceNotFound()
            if (case.tenant_id, event.deduplication_key) in self._event_dedupe:
                return False
            if (
                submission.state != "claimed"
                or submission.deadline is None
                or submission.deadline > now
                or current_case.version != expected_case_version
                or current_attempt.version != expected_attempt_version
                or case.version != expected_case_version + 1
                or attempt.version != expected_attempt_version + 1
            ):
                raise DispatchStateConflict("dispatch_repository_conflict")
            self._validate_submission_outcome("uncertain", case, attempt)
            self._validate_event(event, case=case, attempt=attempt)
            self._cases[case_key] = case
            self._attempts[attempt_key] = attempt
            self._provider_submissions[attempt_key] = replace(
                submission, state="uncertain", completed_at=now
            )
            self._append_event_locked(event)
            return True

    def apply_transition(
        self,
        *,
        case: DispatchCase,
        attempt: CallAttempt,
        event: DispatchEvent,
        expected_case_version: int,
        expected_attempt_version: int,
    ) -> bool:
        with self._lock:
            current_case = self._cases.get((case.tenant_id, case.case_id))
            current_attempt = self._attempts.get(
                (attempt.tenant_id, attempt.attempt_id)
            )
            if current_case is None or current_attempt is None:
                raise DispatchResourceNotFound()
            if (case.tenant_id, event.deduplication_key) in self._event_dedupe:
                return False
            if (
                current_case.version != expected_case_version
                or current_attempt.version != expected_attempt_version
                or case.version != expected_case_version + 1
                or attempt.version != expected_attempt_version + 1
            ):
                raise DispatchStateConflict("dispatch_repository_conflict")
            submission = self._provider_submissions.get(
                (attempt.tenant_id, attempt.attempt_id)
            )
            if (
                event.kind is DispatchEventKind.CANCELED
                and submission is not None
                and (
                    submission.state == "claimed"
                    or (
                        submission.state == "submitted"
                        and current_attempt.provider_call_reference
                        != attempt.provider_call_reference
                    )
                )
            ):
                raise DispatchStateConflict("dispatch_submission_in_flight")
            if (
                attempt.tenant_id != case.tenant_id
                or attempt.case_id != case.case_id
                or current_attempt.case_id != current_case.case_id
            ):
                raise DispatchStateConflict()
            self._validate_event(event, case=case, attempt=attempt)
            self._cases[(case.tenant_id, case.case_id)] = case
            self._attempts[(attempt.tenant_id, attempt.attempt_id)] = attempt
            self._append_event_locked(event)
            return True

    def apply_case_transition(
        self,
        *,
        case: DispatchCase,
        event: DispatchEvent,
        expected_case_version: int,
    ) -> bool:
        with self._lock:
            current = self._cases.get((case.tenant_id, case.case_id))
            if current is None:
                raise DispatchResourceNotFound()
            if (case.tenant_id, event.deduplication_key) in self._event_dedupe:
                return False
            if (
                current.version != expected_case_version
                or case.version != expected_case_version + 1
            ):
                raise DispatchStateConflict("dispatch_repository_conflict")
            self._validate_event(event, case=case, attempt=None)
            self._cases[(case.tenant_id, case.case_id)] = case
            self._append_event_locked(event)
            return True

    def append_event(self, event: DispatchEvent) -> bool:
        with self._lock:
            case = self._cases.get((event.tenant_id, event.case_id))
            if case is None:
                raise DispatchResourceNotFound()
            if (event.tenant_id, event.deduplication_key) in self._event_dedupe:
                return False
            attempt = (
                None
                if event.attempt_id is None
                else self._attempts.get((event.tenant_id, event.attempt_id))
            )
            if event.attempt_id is not None and attempt is None:
                raise DispatchResourceNotFound()
            self._validate_event(event, case=case, attempt=attempt)
            self._append_event_locked(event)
            return True

    def resolve_callback(self, callback_token: str) -> tuple[DispatchCase, CallAttempt]:
        with self._lock:
            mapping = self._callback_index.get(callback_token)
            if mapping is None:
                raise DispatchResourceNotFound()
            tenant_id, attempt_id = mapping
            attempt = self._attempts[(tenant_id, attempt_id)]
            case = self._cases[(tenant_id, attempt.case_id)]
            return case, attempt

    def list_events(self, tenant_id: str, case_id: str) -> tuple[DispatchEvent, ...]:
        with self._lock:
            key = (tenant_id, case_id)
            if key not in self._cases:
                raise DispatchResourceNotFound()
            return tuple(self._events[key])

    def _append_event_locked(self, event: DispatchEvent) -> None:
        self._events[(event.tenant_id, event.case_id)].append(event)
        self._event_dedupe.add((event.tenant_id, event.deduplication_key))

    @staticmethod
    def _provider_reference_matches(stored: str, incoming: str) -> bool:
        if stored.startswith("sha256:"):
            expected = stored.removeprefix("sha256:")
            actual = hashlib.sha256(incoming.encode("utf-8")).hexdigest()
            return len(expected) == 64 and hmac.compare_digest(expected, actual)
        return hmac.compare_digest(stored, incoming)

    @staticmethod
    def _validate_submission_outcome(
        outcome: Literal["submitted", "retryable_failure", "uncertain"],
        case: DispatchCase,
        attempt: CallAttempt,
    ) -> None:
        valid = (
            outcome == "submitted"
            and attempt.status is CallAttemptStatus.INITIATED
            and bool(attempt.provider_call_reference)
            and case.status is DispatchStatus.DIALING
            and case.next_attempt_at is None
        ) or (
            outcome == "retryable_failure"
            and attempt.status is CallAttemptStatus.PROVIDER_RETRY
            and attempt.provider_call_reference is None
            and case.status is DispatchStatus.PROVIDER_RETRY
        ) or (
            outcome == "uncertain"
            and attempt.status is CallAttemptStatus.MANUAL_FOLLOW_UP
            and case.status is DispatchStatus.MANUAL_FOLLOW_UP
        )
        if not valid:
            raise DispatchStateConflict("dispatch_submission_transition_invalid")

    @staticmethod
    def _validate_event(
        event: DispatchEvent,
        *,
        case: DispatchCase,
        attempt: CallAttempt | None,
    ) -> None:
        if event.tenant_id != case.tenant_id or event.case_id != case.case_id:
            raise DispatchStateConflict()
        if attempt is None:
            if event.attempt_id is not None:
                raise DispatchStateConflict()
            return
        if (
            event.attempt_id != attempt.attempt_id
            or attempt.tenant_id != case.tenant_id
            or attempt.case_id != case.case_id
        ):
            raise DispatchStateConflict()
