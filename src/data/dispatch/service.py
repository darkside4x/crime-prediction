"""Human-authorized, bounded voice-escalation coordinator."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hmac import compare_digest

from .directory import ContactDirectory
from .errors import (
    DispatchContactUnavailable,
    DispatchError,
    DispatchIdempotencyConflict,
    DispatchNotAuthorized,
    DispatchRetryNotDue,
    DispatchStateConflict,
    DispatchValidationError,
    VoiceSubmissionUncertain,
    WebhookCallMismatch,
)
from .models import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_DISPATCH_STATUSES,
    CallAttempt,
    CallAttemptStatus,
    ConfirmedIncidentRef,
    DispatchCase,
    DispatchEvent,
    DispatchEventKind,
    DispatchStatus,
    require_identifier,
    require_utc,
)
from .repository import DispatchRepository
from .twilio import CallScript, OutboundCallRequest, VoiceProvider

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_PROVIDER_STATUSES = frozenset(
    {
        "queued",
        "initiated",
        "ringing",
        "answered",
        "in_progress",
        "completed",
        "busy",
        "no_answer",
        "failed",
        "canceled",
    }
)
_TERMINAL_PROVIDER_STATUSES = frozenset(
    {"completed", "busy", "no_answer", "failed", "canceled"}
)
_AMD_RESULTS = frozenset(
    {
        "human",
        "machine_start",
        "machine_end_beep",
        "machine_end_silence",
        "machine_end_other",
        "fax",
        "unknown",
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(*values: str) -> str:
    payload = "\x1f".join(values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class EscalationPolicy:
    """Fixed escalation shape with configurable demo-safe timings."""

    retry_delay: timedelta = timedelta(seconds=30)
    ring_timeout_seconds: int = 20
    gather_timeout_seconds: int = 10
    provider_submission_timeout: timedelta = timedelta(seconds=90)
    policy_version: str = "dispatch-escalation-v1"
    message_template_version: str = "dispatch-voice-v1"

    def __post_init__(self) -> None:
        if not timedelta(seconds=5) <= self.retry_delay <= timedelta(hours=1):
            raise DispatchValidationError(
                "dispatch_policy_invalid", "Retry delay is invalid"
            )
        if not 5 <= self.ring_timeout_seconds <= 60:
            raise DispatchValidationError(
                "dispatch_policy_invalid", "Ring timeout is invalid"
            )
        if not 3 <= self.gather_timeout_seconds <= 30:
            raise DispatchValidationError(
                "dispatch_policy_invalid", "Gather timeout is invalid"
            )
        if not timedelta(seconds=30) <= self.provider_submission_timeout <= timedelta(
            hours=12
        ):
            raise DispatchValidationError(
                "dispatch_policy_invalid", "Provider submission timeout is invalid"
            )
        require_identifier(self.policy_version, "policy_version")
        require_identifier(self.message_template_version, "message_template_version")

    @property
    def primary_attempts(self) -> int:
        return 2

    @property
    def supervisor_attempts(self) -> int:
        return 1

    @property
    def max_attempts(self) -> int:
        return 3


class DispatchCoordinator:
    """Coordinates authorization, logical attempts, and provider callbacks.

    The repository reserves a logical attempt before the provider is invoked.
    A retryable provider failure reuses that attempt. Only an unacknowledged,
    completed logical call advances the 2-primary/1-supervisor sequence.
    """

    def __init__(
        self,
        repository: DispatchRepository,
        directory: ContactDirectory,
        voice_provider: VoiceProvider,
        *,
        policy: EscalationPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] | None = None,
        token_factory: Callable[[str], str] | None = None,
        claim_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.directory = directory
        self.voice_provider = voice_provider
        self.policy = policy or EscalationPolicy()
        self._clock = clock
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._token_factory = token_factory or (
            lambda _attempt_id: secrets.token_urlsafe(32)
        )
        self._claim_factory = claim_factory or (lambda: secrets.token_urlsafe(32))

    def authorize(
        self,
        incident: ConfirmedIncidentRef,
        *,
        authorized_by: str,
        idempotency_key: str,
        authorize_call: bool,
        message_template_version: str | None = None,
    ) -> DispatchCase:
        if authorize_call is not True or incident.review_decision != "confirmed":
            raise DispatchNotAuthorized()
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise DispatchValidationError(
                "dispatch_idempotency_key_invalid", "Idempotency key is invalid"
            )
        if not authorized_by or len(authorized_by) > 128:
            raise DispatchValidationError(
                "dispatch_authorization_invalid", "Authorizing principal is invalid"
            )
        template_version = (
            message_template_version or self.policy.message_template_version
        )
        require_identifier(template_version, "message_template_version")
        fingerprint = self._authorization_fingerprint(
            incident, authorized_by, template_version
        )
        existing = self.repository.find_case_by_idempotency(
            incident.tenant_id, idempotency_key
        )
        if existing is not None:
            if not compare_digest(existing.authorization_fingerprint, fingerprint):
                raise DispatchIdempotencyConflict()
            return existing

        now = require_utc(self._clock(), "authorization time")
        contacts = self.directory.resolve(incident.tenant_id, incident.coverage, at=now)
        case = DispatchCase(
            case_id=self._id_factory(),
            tenant_id=incident.tenant_id,
            incident_id=incident.incident_id,
            confirmed_review_id=incident.confirmed_review_id,
            incident_source_id=incident.incident_source_id,
            incident_external_event_id=incident.incident_external_event_id,
            zone_id=incident.zone_id,
            primary_contact_id=contacts.primary.contact_id,
            supervisor_contact_id=contacts.supervisor.contact_id,
            case_reference=incident.case_reference,
            category=incident.category,
            broad_location_label=incident.broad_location_label,
            occurred_at=require_utc(incident.occurred_at, "occurred_at"),
            authorized_by=authorized_by,
            authorization_fingerprint=fingerprint,
            idempotency_key_hash=_digest(idempotency_key),
            policy_version=self.policy.policy_version,
            message_template_version=template_version,
            retry_delay_seconds=int(self.policy.retry_delay.total_seconds()),
            status=DispatchStatus.QUEUED,
            created_at=now,
            updated_at=now,
            next_attempt_at=now,
            attempt_count=0,
            final_outcome=None,
            closed_at=None,
        )
        event = self._event(
            case,
            kind=DispatchEventKind.AUTHORIZED,
            at=now,
            dedupe_parts=("authorized", incident.tenant_id, idempotency_key),
        )
        stored, _ = self.repository.create_case(
            case, idempotency_key=idempotency_key, event=event
        )
        if not compare_digest(stored.authorization_fingerprint, fingerprint):
            raise DispatchIdempotencyConflict()
        return stored

    def dispatch_next(self, tenant_id: str, case_id: str) -> CallAttempt | None:
        case = self.repository.get_case(tenant_id, case_id)
        if case.status in TERMINAL_DISPATCH_STATUSES:
            return None
        now = require_utc(self._clock(), "dispatch time")
        attempt_id = self._id_factory()
        callback_token = self._token_factory(attempt_id)
        reservation_event = self._event(
            case,
            kind=DispatchEventKind.ATTEMPT_RESERVED,
            at=now,
            attempt_id=attempt_id,
            dedupe_parts=("attempt-reserved", tenant_id, case_id, attempt_id),
        )
        reservation = self.repository.reserve_next_attempt(
            tenant_id,
            case_id,
            attempt_id=attempt_id,
            callback_token=callback_token,
            at=now,
            event=reservation_event,
        )
        case, attempt = reservation.case, reservation.attempt
        if attempt.status in {
            CallAttemptStatus.INITIATED,
            CallAttemptStatus.RINGING,
            CallAttemptStatus.ANSWERED,
        }:
            return attempt
        if attempt.status not in {
            CallAttemptStatus.RESERVED,
            CallAttemptStatus.PROVIDER_RETRY,
        }:
            raise DispatchStateConflict()

        contact = self.directory.get_contact(tenant_id, attempt.contact_id)
        if not contact.is_available(now) or contact.role is not attempt.target_role:
            raise DispatchContactUnavailable(retryable=True)
        claim_token = self._claim_factory()
        claim = self.repository.claim_provider_submission(
            tenant_id,
            case_id,
            attempt.attempt_id,
            claim_token=claim_token,
            at=now,
            deadline=now + self.policy.provider_submission_timeout,
        )
        case, attempt = claim.case, claim.attempt
        if case.status in TERMINAL_DISPATCH_STATUSES:
            return None
        if claim.stale:
            self._mark_submission_uncertain(
                case,
                attempt,
                now,
                safe_error_code="voice_submission_claim_expired",
                stale=True,
            )
            return None
        if not claim.acquired:
            if claim.retry_at is not None:
                raise DispatchRetryNotDue()
            if attempt.status in {
                CallAttemptStatus.INITIATED,
                CallAttemptStatus.RINGING,
                CallAttemptStatus.ANSWERED,
            }:
                return attempt
            raise DispatchStateConflict("dispatch_submission_claim_unavailable")
        request = OutboundCallRequest(
            request_id=attempt.attempt_id,
            destination_secret_ref=contact.phone_secret_ref,
            callback_token=attempt.callback_token,
            attempt_number=attempt.sequence,
            target_role=attempt.target_role,
            script=self._script(case),
            ring_timeout_seconds=self.policy.ring_timeout_seconds,
        )
        try:
            result = self.voice_provider.place_call(request)
        except VoiceSubmissionUncertain:
            self._mark_submission_uncertain(
                case,
                attempt,
                now,
                safe_error_code="voice_submission_uncertain",
                claim_token=claim_token,
            )
            return None
        except DispatchError as error:
            self._mark_provider_retry(
                case,
                attempt,
                now,
                claim_token=claim_token,
                safe_error_code=error.code,
            )
            raise
        except Exception:  # noqa: BLE001 - provider boundary is normalized
            self._mark_submission_uncertain(
                case,
                attempt,
                now,
                safe_error_code="voice_submission_uncertain",
                claim_token=claim_token,
            )
            return None

        updated_attempt = replace(
            attempt,
            provider_call_reference=result.provider_call_reference,
            status=CallAttemptStatus.INITIATED,
            initiated_at=attempt.initiated_at or now,
            safe_error_code=None,
            updated_at=now,
            version=attempt.version + 1,
        )
        updated_case = replace(
            case,
            status=DispatchStatus.DIALING,
            next_attempt_at=None,
            updated_at=now,
            version=case.version + 1,
        )
        event = self._event(
            case,
            kind=DispatchEventKind.CALL_INITIATED,
            at=now,
            attempt_id=attempt.attempt_id,
            dedupe_parts=("call-initiated", attempt.attempt_id),
        )
        self.repository.finish_provider_submission(
            case=updated_case,
            attempt=updated_attempt,
            event=event,
            claim_token=claim_token,
            outcome="submitted",
            expected_case_version=case.version,
            expected_attempt_version=attempt.version,
        )
        return updated_attempt

    def handle_status(
        self,
        callback_token: str,
        *,
        provider_call_reference: str,
        status: str,
        event_key: str,
        occurred_at: datetime | None = None,
    ) -> DispatchCase:
        normalized = status.strip().lower().replace("-", "_")
        if normalized not in _PROVIDER_STATUSES:
            raise DispatchValidationError(
                "twilio_status_invalid", "Provider call status is invalid"
            )
        case, attempt = self._resolve_callback(callback_token, provider_call_reference)
        now = self._callback_time(occurred_at)
        dedupe = (
            "provider-status",
            provider_call_reference,
            normalized,
        )
        if (
            case.status in TERMINAL_DISPATCH_STATUSES
            or attempt.status in TERMINAL_ATTEMPT_STATUSES
        ):
            self.repository.append_event(
                self._event(
                    case,
                    kind=DispatchEventKind.PROVIDER_STATUS,
                    at=now,
                    attempt_id=attempt.attempt_id,
                    dedupe_parts=dedupe,
                    detail_code=normalized,
                )
            )
            return self.repository.get_case(case.tenant_id, case.case_id)

        if normalized in _TERMINAL_PROVIDER_STATUSES:
            updated_case, updated_attempt, kind = self._unacknowledged(
                case,
                attempt,
                now,
                outcome={
                    "completed": "no_acknowledgement",
                    "busy": "busy",
                    "no_answer": "no_answer",
                    "failed": "failed",
                    "canceled": "canceled",
                }[normalized],
            )
        else:
            new_status = self._progress_status(attempt.status, normalized)
            case_status = (
                DispatchStatus.AWAITING_ACKNOWLEDGEMENT
                if new_status is CallAttemptStatus.ANSWERED
                else DispatchStatus.DIALING
            )
            updated_attempt = replace(
                attempt,
                status=new_status,
                initiated_at=(
                    attempt.initiated_at
                    or (now if new_status is not CallAttemptStatus.RESERVED else None)
                ),
                answered_at=(
                    attempt.answered_at
                    or (now if new_status is CallAttemptStatus.ANSWERED else None)
                ),
                updated_at=now,
                version=attempt.version + 1,
            )
            updated_case = replace(
                case,
                status=case_status,
                updated_at=now,
                version=case.version + 1,
            )
            kind = DispatchEventKind.PROVIDER_STATUS
        event = self._event(
            case,
            kind=kind,
            at=now,
            attempt_id=attempt.attempt_id,
            dedupe_parts=dedupe,
            detail_code=normalized,
        )
        self.repository.apply_transition(
            case=updated_case,
            attempt=updated_attempt,
            event=event,
            expected_case_version=case.version,
            expected_attempt_version=attempt.version,
        )
        return self.repository.get_case(case.tenant_id, case.case_id)

    def handle_gather(
        self,
        callback_token: str,
        *,
        provider_call_reference: str,
        digits: str,
        event_key: str,
        occurred_at: datetime | None = None,
    ) -> DispatchCase:
        case, attempt = self._resolve_callback(callback_token, provider_call_reference)
        now = self._callback_time(occurred_at)
        dedupe = (
            "gather",
            provider_call_reference,
            digits,
        )
        if digits not in {"1", "2"}:
            self.repository.append_event(
                self._event(
                    case,
                    kind=DispatchEventKind.INVALID_GATHER_INPUT,
                    at=now,
                    attempt_id=attempt.attempt_id,
                    dedupe_parts=dedupe,
                )
            )
            return self.repository.get_case(case.tenant_id, case.case_id)

        desired_case = (
            DispatchStatus.ACKNOWLEDGED
            if digits == "1"
            else DispatchStatus.MANUAL_FOLLOW_UP
        )
        desired_attempt = (
            CallAttemptStatus.ACKNOWLEDGED
            if digits == "1"
            else CallAttemptStatus.MANUAL_FOLLOW_UP
        )
        kind = (
            DispatchEventKind.ACKNOWLEDGED
            if digits == "1"
            else DispatchEventKind.MANUAL_FOLLOW_UP
        )
        if case.status in {
            DispatchStatus.ACKNOWLEDGED,
            DispatchStatus.MANUAL_FOLLOW_UP,
            DispatchStatus.CANCELED,
        }:
            self.repository.append_event(
                self._event(
                    case,
                    kind=kind,
                    at=now,
                    attempt_id=attempt.attempt_id,
                    dedupe_parts=dedupe,
                )
            )
            return self.repository.get_case(case.tenant_id, case.case_id)
        updated_attempt = replace(
            attempt,
            status=desired_attempt,
            outcome="acknowledged" if digits == "1" else "callback_requested",
            completed_at=now,
            next_action_at=None,
            updated_at=now,
            version=attempt.version + 1,
        )
        updated_case = replace(
            case,
            status=desired_case,
            next_attempt_at=None,
            final_outcome=desired_case.value,
            closed_at=now,
            updated_at=now,
            version=case.version + 1,
        )
        self.repository.apply_transition(
            case=updated_case,
            attempt=updated_attempt,
            event=self._event(
                case,
                kind=kind,
                at=now,
                attempt_id=attempt.attempt_id,
                dedupe_parts=dedupe,
            ),
            expected_case_version=case.version,
            expected_attempt_version=attempt.version,
        )
        return self.repository.get_case(case.tenant_id, case.case_id)

    def handle_answering_machine(
        self,
        callback_token: str,
        *,
        provider_call_reference: str,
        result: str,
        event_key: str,
        occurred_at: datetime | None = None,
    ) -> DispatchCase:
        normalized = result.strip().lower().replace("-", "_")
        if normalized not in _AMD_RESULTS:
            raise DispatchValidationError(
                "twilio_amd_result_invalid", "Answering-machine result is invalid"
            )
        case, attempt = self._resolve_callback(callback_token, provider_call_reference)
        now = self._callback_time(occurred_at)
        dedupe = (
            "amd",
            provider_call_reference,
            normalized,
        )
        if (
            case.status in TERMINAL_DISPATCH_STATUSES
            or attempt.status in TERMINAL_ATTEMPT_STATUSES
        ):
            self.repository.append_event(
                self._event(
                    case,
                    kind=DispatchEventKind.ANSWERING_MACHINE,
                    at=now,
                    attempt_id=attempt.attempt_id,
                    dedupe_parts=dedupe,
                    detail_code=normalized,
                )
            )
            return self.repository.get_case(case.tenant_id, case.case_id)
        if normalized == "human":
            self.repository.append_event(
                self._event(
                    case,
                    kind=DispatchEventKind.ANSWERING_MACHINE,
                    at=now,
                    attempt_id=attempt.attempt_id,
                    dedupe_parts=dedupe,
                    detail_code=normalized,
                )
            )
            return case

        # Machine, fax, and unknown never count as acknowledgement. Unknown is
        # deliberately conservative instead of being treated as a person.
        updated_case, updated_attempt, kind = self._unacknowledged(
            case, attempt, now, outcome="no_acknowledgement"
        )
        self.repository.apply_transition(
            case=updated_case,
            attempt=updated_attempt,
            event=self._event(
                case,
                kind=kind,
                at=now,
                attempt_id=attempt.attempt_id,
                dedupe_parts=dedupe,
                detail_code=normalized,
            ),
            expected_case_version=case.version,
            expected_attempt_version=attempt.version,
        )
        return self.repository.get_case(case.tenant_id, case.case_id)

    def cancel(
        self,
        tenant_id: str,
        case_id: str,
        *,
        canceled_by: str,
        event_key: str,
    ) -> DispatchCase:
        if not canceled_by or len(canceled_by) > 256:
            raise DispatchValidationError(
                "dispatch_cancellation_invalid", "Canceling principal is invalid"
            )
        case = self.repository.get_case(tenant_id, case_id)
        if case.status in TERMINAL_DISPATCH_STATUSES:
            return case
        now = require_utc(self._clock(), "cancellation time")
        attempts = self.repository.list_attempts(tenant_id, case_id)
        active = [
            item for item in attempts if item.status not in TERMINAL_ATTEMPT_STATUSES
        ]
        event = self._event(
            case,
            kind=DispatchEventKind.CANCELED,
            at=now,
            attempt_id=active[-1].attempt_id if active else None,
            dedupe_parts=("cancel", tenant_id, case_id, event_key),
        )
        updated_case = replace(
            case,
            status=DispatchStatus.CANCELED,
            next_attempt_at=None,
            final_outcome=DispatchStatus.CANCELED.value,
            closed_at=now,
            updated_at=now,
            version=case.version + 1,
        )
        if active:
            attempt = active[-1]
            updated_attempt = replace(
                attempt,
                status=CallAttemptStatus.CANCELED,
                outcome="canceled",
                completed_at=now,
                next_action_at=None,
                updated_at=now,
                version=attempt.version + 1,
            )
            self.repository.apply_transition(
                case=updated_case,
                attempt=updated_attempt,
                event=event,
                expected_case_version=case.version,
                expected_attempt_version=attempt.version,
            )
            # Production persistence deliberately retains only a hash of the
            # provider Call SID. That is sufficient to authenticate callbacks
            # but cannot address an already-active provider call. Cancellation
            # still closes the case and prevents every later attempt.
            if attempt.provider_call_reference and not attempt.provider_call_reference.startswith(
                "sha256:"
            ):
                self.voice_provider.cancel_call(attempt.provider_call_reference)
        else:
            self.repository.apply_case_transition(
                case=updated_case,
                event=event,
                expected_case_version=case.version,
            )
        return self.repository.get_case(tenant_id, case_id)

    def mark_delivery_exhausted(
        self,
        tenant_id: str,
        case_id: str,
    ) -> DispatchCase:
        """Stop automation when the durable queue exhausts its deliveries.

        This transition is deliberately safe and idempotent.  It never starts
        or cancels a provider call: an operator must inspect the case because a
        concurrently submitted provider request may have an uncertain outcome.
        """

        for conflict_retry in range(2):
            case = self.repository.get_case(tenant_id, case_id)
            if case.status in TERMINAL_DISPATCH_STATUSES:
                return case
            now = require_utc(self._clock(), "delivery exhaustion time")
            attempts = self.repository.list_attempts(tenant_id, case_id)
            active = [
                item
                for item in attempts
                if item.status not in TERMINAL_ATTEMPT_STATUSES
            ]
            attempt = active[-1] if active else None
            event = self._event(
                case,
                kind=DispatchEventKind.MANUAL_FOLLOW_UP,
                at=now,
                attempt_id=None if attempt is None else attempt.attempt_id,
                dedupe_parts=("delivery-exhausted", tenant_id, case_id),
                detail_code="dispatch_delivery_exhausted",
            )
            updated_case = replace(
                case,
                status=DispatchStatus.MANUAL_FOLLOW_UP,
                next_attempt_at=None,
                final_outcome=DispatchStatus.MANUAL_FOLLOW_UP.value,
                closed_at=now,
                updated_at=now,
                version=case.version + 1,
            )
            try:
                if attempt is None:
                    self.repository.apply_case_transition(
                        case=updated_case,
                        event=event,
                        expected_case_version=case.version,
                    )
                else:
                    updated_attempt = replace(
                        attempt,
                        status=CallAttemptStatus.MANUAL_FOLLOW_UP,
                        outcome="failed",
                        completed_at=attempt.completed_at or now,
                        next_action_at=None,
                        safe_error_code="dispatch_delivery_exhausted",
                        updated_at=now,
                        version=attempt.version + 1,
                    )
                    self.repository.apply_transition(
                        case=updated_case,
                        attempt=updated_attempt,
                        event=event,
                        expected_case_version=case.version,
                        expected_attempt_version=attempt.version,
                    )
            except DispatchStateConflict as error:
                if error.code == "dispatch_repository_conflict" and conflict_retry == 0:
                    continue
                raise
            return self.repository.get_case(tenant_id, case_id)
        raise DispatchStateConflict("dispatch_repository_conflict")  # pragma: no cover

    def voice_script(self, callback_token: str) -> CallScript:
        case, _ = self.repository.resolve_callback(callback_token)
        if case.status in TERMINAL_DISPATCH_STATUSES:
            raise DispatchStateConflict("dispatch_case_terminal")
        return self._script(case)

    def _mark_provider_retry(
        self,
        case: DispatchCase,
        attempt: CallAttempt,
        at: datetime,
        *,
        claim_token: str,
        safe_error_code: str,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,80}", safe_error_code):
            safe_error_code = "voice_provider_unavailable"
        updated_attempt = replace(
            attempt,
            status=CallAttemptStatus.PROVIDER_RETRY,
            safe_error_code=safe_error_code,
            updated_at=at,
            version=attempt.version + 1,
        )
        updated_case = replace(
            case,
            status=DispatchStatus.PROVIDER_RETRY,
            next_attempt_at=at,
            updated_at=at,
            version=case.version + 1,
        )
        self.repository.finish_provider_submission(
            case=updated_case,
            attempt=updated_attempt,
            event=self._event(
                case,
                kind=DispatchEventKind.PROVIDER_RETRY,
                at=at,
                attempt_id=attempt.attempt_id,
                dedupe_parts=(
                    "provider-retry",
                    attempt.attempt_id,
                    str(attempt.version + 1),
                ),
                detail_code=safe_error_code,
            ),
            claim_token=claim_token,
            outcome="retryable_failure",
            expected_case_version=case.version,
            expected_attempt_version=attempt.version,
        )

    def _mark_submission_uncertain(
        self,
        case: DispatchCase,
        attempt: CallAttempt,
        at: datetime,
        *,
        safe_error_code: str,
        claim_token: str | None = None,
        stale: bool = False,
    ) -> None:
        updated_attempt = replace(
            attempt,
            status=CallAttemptStatus.MANUAL_FOLLOW_UP,
            outcome="callback_requested",
            completed_at=at,
            next_action_at=None,
            safe_error_code=safe_error_code,
            updated_at=at,
            version=attempt.version + 1,
        )
        updated_case = replace(
            case,
            status=DispatchStatus.MANUAL_FOLLOW_UP,
            next_attempt_at=None,
            final_outcome=DispatchStatus.MANUAL_FOLLOW_UP.value,
            closed_at=at,
            updated_at=at,
            version=case.version + 1,
        )
        event = self._event(
            case,
            kind=DispatchEventKind.MANUAL_FOLLOW_UP,
            at=at,
            attempt_id=attempt.attempt_id,
            dedupe_parts=("provider-submission-uncertain", attempt.attempt_id),
            detail_code=safe_error_code,
        )
        if stale:
            self.repository.expire_provider_submission(
                case=updated_case,
                attempt=updated_attempt,
                event=event,
                expired_at=at,
                expected_case_version=case.version,
                expected_attempt_version=attempt.version,
            )
            return
        if claim_token is None:
            raise DispatchStateConflict("dispatch_submission_claim_unavailable")
        self.repository.finish_provider_submission(
            case=updated_case,
            attempt=updated_attempt,
            event=event,
            claim_token=claim_token,
            outcome="uncertain",
            expected_case_version=case.version,
            expected_attempt_version=attempt.version,
        )

    def _unacknowledged(
        self,
        case: DispatchCase,
        attempt: CallAttempt,
        at: datetime,
        *,
        outcome: str,
    ) -> tuple[DispatchCase, CallAttempt, DispatchEventKind]:
        if attempt.sequence == 1:
            status = DispatchStatus.RETRY_SCHEDULED
            kind = DispatchEventKind.RETRY_SCHEDULED
            next_attempt = at + self.policy.retry_delay
        elif attempt.sequence == 2:
            status = DispatchStatus.SUPERVISOR_SCHEDULED
            kind = DispatchEventKind.SUPERVISOR_SCHEDULED
            next_attempt = at + self.policy.retry_delay
        else:
            status = DispatchStatus.UNACKNOWLEDGED
            kind = DispatchEventKind.EXHAUSTED
            next_attempt = None
        updated_attempt = replace(
            attempt,
            status=CallAttemptStatus.UNACKNOWLEDGED,
            outcome=outcome,
            completed_at=at,
            next_action_at=next_attempt,
            updated_at=at,
            version=attempt.version + 1,
        )
        updated_case = replace(
            case,
            status=status,
            next_attempt_at=next_attempt,
            final_outcome=(
                DispatchStatus.UNACKNOWLEDGED.value
                if status is DispatchStatus.UNACKNOWLEDGED
                else None
            ),
            closed_at=at if status is DispatchStatus.UNACKNOWLEDGED else None,
            updated_at=at,
            version=case.version + 1,
        )
        return updated_case, updated_attempt, kind

    @staticmethod
    def _progress_status(
        current: CallAttemptStatus, provider_status: str
    ) -> CallAttemptStatus:
        desired = {
            "queued": CallAttemptStatus.INITIATED,
            "initiated": CallAttemptStatus.INITIATED,
            "ringing": CallAttemptStatus.RINGING,
            "answered": CallAttemptStatus.ANSWERED,
            "in_progress": CallAttemptStatus.ANSWERED,
        }[provider_status]
        ranks = {
            CallAttemptStatus.RESERVED: 0,
            CallAttemptStatus.PROVIDER_RETRY: 0,
            CallAttemptStatus.INITIATED: 1,
            CallAttemptStatus.RINGING: 2,
            CallAttemptStatus.ANSWERED: 3,
        }
        return desired if ranks[desired] > ranks.get(current, -1) else current

    def _resolve_callback(
        self, callback_token: str, provider_call_reference: str
    ) -> tuple[DispatchCase, CallAttempt]:
        case, attempt = self.repository.resolve_callback(callback_token)
        stored_reference = attempt.provider_call_reference
        matches = False
        if stored_reference and provider_call_reference:
            if stored_reference.startswith("sha256:"):
                expected_hash = stored_reference.removeprefix("sha256:")
                incoming_hash = hashlib.sha256(
                    provider_call_reference.encode("utf-8")
                ).hexdigest()
                matches = bool(
                    re.fullmatch(r"[a-f0-9]{64}", expected_hash)
                    and compare_digest(expected_hash, incoming_hash)
                )
            else:
                matches = compare_digest(stored_reference, provider_call_reference)
        if not matches:
            raise WebhookCallMismatch()
        return case, attempt

    def _callback_time(self, occurred_at: datetime | None) -> datetime:
        return require_utc(
            occurred_at if occurred_at is not None else self._clock(),
            "callback time",
        )

    def _authorization_fingerprint(
        self,
        incident: ConfirmedIncidentRef,
        authorized_by: str,
        message_template_version: str,
    ) -> str:
        payload = json.dumps(
            {
                "tenant_id": incident.tenant_id,
                "incident_id": incident.incident_id,
                "confirmed_review_id": incident.confirmed_review_id,
                "incident_source_id": incident.incident_source_id,
                "incident_external_event_id": incident.incident_external_event_id,
                "authorized_by": authorized_by,
                "message_template_version": message_template_version,
                "authorize_call": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _script(case: DispatchCase) -> CallScript:
        return CallScript(
            case_reference=case.case_reference,
            category=case.category,
            broad_location_label=case.broad_location_label,
            occurred_at=case.occurred_at,
        )

    def _event(
        self,
        case: DispatchCase,
        *,
        kind: DispatchEventKind,
        at: datetime,
        dedupe_parts: tuple[str, ...],
        attempt_id: str | None = None,
        detail_code: str | None = None,
    ) -> DispatchEvent:
        return DispatchEvent(
            event_id=self._id_factory(),
            tenant_id=case.tenant_id,
            case_id=case.case_id,
            attempt_id=attempt_id,
            kind=kind,
            occurred_at=at,
            deduplication_key=_digest(*dedupe_parts),
            detail_code=detail_code,
        )
