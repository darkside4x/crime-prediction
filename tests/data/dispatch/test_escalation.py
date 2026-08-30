from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from src.data.dispatch import (
    CallAttemptStatus,
    ConfirmedIncidentRef,
    ContactRole,
    DispatchCoordinator,
    DispatchEvent,
    DispatchEventKind,
    DispatchIdempotencyConflict,
    DispatchNotAuthorized,
    DispatchResourceNotFound,
    DispatchRetryNotDue,
    DispatchStateConflict,
    DispatchStatus,
    EscalationPolicy,
    InMemoryContactDirectory,
    InMemoryDispatchRepository,
    MockTwilioVoiceProvider,
    OutboundCallResult,
    ResponseContact,
    VoiceProviderUnavailable,
    VoiceSubmissionUncertain,
    WebhookCallMismatch,
)

TENANT_A = "00000000-0000-4000-8000-000000000001"
TENANT_B = "00000000-0000-4000-8000-000000000002"
CELL = "8860145b49fffff"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 30, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _contact(contact_id: str, role: ContactRole) -> ResponseContact:
    return ResponseContact(
        contact_id=contact_id,
        tenant_id=TENANT_A,
        zone_id="demo-zone-a",
        role=role,
        phone_secret_ref=f"secret://dispatch/{contact_id}",
        display_name=f"Demo {role.value}",
        opted_in_at=datetime(2026, 8, 1, tzinfo=UTC),
        verified_at=datetime(2026, 8, 1, tzinfo=UTC),
        coverage_cells=frozenset({CELL}),
    )


def _incident(
    *, suffix: str = "1", decision: str = "confirmed"
) -> ConfirmedIncidentRef:
    return ConfirmedIncidentRef(
        tenant_id=TENANT_A,
        incident_id=f"incident-{suffix}",
        confirmed_review_id=f"review-{suffix}",
        incident_source_id=f"source-{suffix}",
        incident_external_event_id=f"external-event-{suffix}",
        cell_id=CELL,
        zone_id="demo-zone-a",
        case_reference=f"CH-{suffix}",
        category="traffic_safety",
        broad_location_label="Demo Zone A",
        occurred_at=datetime(2026, 8, 30, 11, 55, tzinfo=UTC),
        review_decision=decision,
    )


def _system(*, failures: int = 0, retry_seconds: int = 30):
    clock = Clock()
    repository = InMemoryDispatchRepository()
    directory = InMemoryContactDirectory(
        [
            _contact("primary-a", ContactRole.PRIMARY),
            _contact("supervisor-a", ContactRole.SUPERVISOR),
        ]
    )
    provider = MockTwilioVoiceProvider(failures_before_success=failures)
    coordinator = DispatchCoordinator(
        repository,
        directory,
        provider,
        policy=EscalationPolicy(retry_delay=timedelta(seconds=retry_seconds)),
        clock=clock,
    )
    return coordinator, repository, provider, clock


def _authorize(coordinator: DispatchCoordinator, *, key: str = "dispatch-key-0001"):
    return coordinator.authorize(
        _incident(),
        authorized_by="reviewer-subject-a",
        idempotency_key=key,
        authorize_call=True,
    )


def _complete(
    coordinator: DispatchCoordinator,
    attempt,
    *,
    event_key: str,
) -> None:
    coordinator.handle_status(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        status="completed",
        event_key=event_key,
    )


def test_authorization_requires_confirmed_incident_and_explicit_call_consent() -> None:
    coordinator, repository, _, _ = _system()
    with pytest.raises(DispatchNotAuthorized):
        coordinator.authorize(
            _incident(decision="rejected"),
            authorized_by="reviewer-subject-a",
            idempotency_key="dispatch-key-0001",
            authorize_call=True,
        )
    with pytest.raises(DispatchNotAuthorized):
        coordinator.authorize(
            _incident(),
            authorized_by="reviewer-subject-a",
            idempotency_key="dispatch-key-0001",
            authorize_call=False,
        )
    assert repository.find_case_by_idempotency(TENANT_A, "dispatch-key-0001") is None


def test_authorization_is_idempotent_and_conflicting_reuse_is_rejected() -> None:
    coordinator, _, provider, _ = _system()
    first = _authorize(coordinator)
    second = _authorize(coordinator)
    assert second == first
    assert provider.requests == ()

    with pytest.raises(DispatchIdempotencyConflict):
        coordinator.authorize(
            _incident(suffix="2"),
            authorized_by="reviewer-subject-a",
            idempotency_key="dispatch-key-0001",
            authorize_call=True,
        )


def test_exactly_two_primary_calls_then_one_supervisor_and_stop() -> None:
    coordinator, repository, provider, clock = _system()
    case = _authorize(coordinator)

    first = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert first is not None
    assert first.sequence == 1
    assert first.target_role is ContactRole.PRIMARY
    _complete(coordinator, first, event_key="status-1")

    with pytest.raises(DispatchRetryNotDue):
        coordinator.dispatch_next(TENANT_A, case.case_id)
    clock.advance(30)
    second = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert second is not None
    assert second.sequence == 2
    assert second.target_role is ContactRole.PRIMARY
    _complete(coordinator, second, event_key="status-2")

    clock.advance(30)
    third = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert third is not None
    assert third.sequence == 3
    assert third.target_role is ContactRole.SUPERVISOR
    _complete(coordinator, third, event_key="status-3")

    final = repository.get_case(TENANT_A, case.case_id)
    assert final.status is DispatchStatus.UNACKNOWLEDGED
    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    assert [request.target_role for request in provider.requests] == [
        ContactRole.PRIMARY,
        ContactRole.PRIMARY,
        ContactRole.SUPERVISOR,
    ]
    assert len(repository.list_attempts(TENANT_A, case.case_id)) == 3


def test_press_one_acknowledges_idempotently_and_stops_all_future_calls() -> None:
    coordinator, repository, provider, clock = _system()
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None

    acknowledged = coordinator.handle_gather(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        digits="1",
        event_key="gather-1",
    )
    duplicate = coordinator.handle_gather(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        digits="1",
        event_key="gather-1",
    )
    coordinator.handle_status(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        status="completed",
        event_key="completed-after-ack",
    )
    clock.advance(300)

    assert acknowledged.status is DispatchStatus.ACKNOWLEDGED
    assert duplicate.status is DispatchStatus.ACKNOWLEDGED
    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    assert len(provider.requests) == 1
    assert (
        repository.list_attempts(TENANT_A, case.case_id)[0].status
        is CallAttemptStatus.ACKNOWLEDGED
    )


def test_press_two_stops_automation_and_flags_manual_follow_up() -> None:
    coordinator, _, provider, clock = _system()
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None

    updated = coordinator.handle_gather(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        digits="2",
        event_key="gather-2",
    )
    clock.advance(300)

    assert updated.status is DispatchStatus.MANUAL_FOLLOW_UP
    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    assert len(provider.requests) == 1


def test_provider_retry_reuses_one_logical_attempt() -> None:
    coordinator, repository, provider, _ = _system(failures=1)
    case = _authorize(coordinator)

    with pytest.raises(VoiceProviderUnavailable):
        coordinator.dispatch_next(TENANT_A, case.case_id)
    stored = repository.list_attempts(TENANT_A, case.case_id)
    assert len(stored) == 1
    assert stored[0].status is CallAttemptStatus.PROVIDER_RETRY

    succeeded = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert succeeded is not None
    assert succeeded.sequence == 1
    assert len(repository.list_attempts(TENANT_A, case.case_id)) == 1
    assert provider.place_call_invocations == 2


def test_concurrent_worker_delivery_cannot_create_duplicate_logical_calls() -> None:
    coordinator, repository, provider, _ = _system()
    case = _authorize(coordinator)

    with ThreadPoolExecutor(max_workers=4) as executor:
        attempts = list(
            executor.map(
                lambda _item: coordinator.dispatch_next(TENANT_A, case.case_id),
                range(4),
            )
        )

    assert {item.attempt_id for item in attempts if item is not None} == {
        repository.list_attempts(TENANT_A, case.case_id)[0].attempt_id
    }
    assert len(repository.list_attempts(TENANT_A, case.case_id)) == 1
    assert len(provider.requests) == 1


def test_two_workers_cannot_submit_the_same_provider_call_concurrently() -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()
            self.invocations = 0

        def place_call(self, request):
            del request
            self.invocations += 1
            self.started.set()
            assert self.release.wait(timeout=5)
            return OutboundCallResult("mock-blocking-call")

        def cancel_call(self, provider_call_reference: str) -> None:
            del provider_call_reference

    clock = Clock()
    repository = InMemoryDispatchRepository()
    directory = InMemoryContactDirectory(
        [
            _contact("primary-a", ContactRole.PRIMARY),
            _contact("supervisor-a", ContactRole.SUPERVISOR),
        ]
    )
    provider = BlockingProvider()
    worker_one = DispatchCoordinator(repository, directory, provider, clock=clock)
    worker_two = DispatchCoordinator(repository, directory, provider, clock=clock)
    case = _authorize(worker_one)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(worker_one.dispatch_next, TENANT_A, case.case_id)
        assert provider.started.wait(timeout=5)
        with pytest.raises(DispatchRetryNotDue):
            worker_two.dispatch_next(TENANT_A, case.case_id)
        provider.release.set()
        attempt = first.result(timeout=5)

    assert attempt is not None
    assert provider.invocations == 1
    assert len(repository.list_attempts(TENANT_A, case.case_id)) == 1


def test_stale_provider_submission_claim_closes_for_manual_follow_up() -> None:
    class WorkerCrash(BaseException):
        pass

    class CrashingProvider:
        def __init__(self) -> None:
            self.invocations = 0

        def place_call(self, request):
            del request
            self.invocations += 1
            raise WorkerCrash()

        def cancel_call(self, provider_call_reference: str) -> None:
            del provider_call_reference

    clock = Clock()
    repository = InMemoryDispatchRepository()
    directory = InMemoryContactDirectory(
        [
            _contact("primary-a", ContactRole.PRIMARY),
            _contact("supervisor-a", ContactRole.SUPERVISOR),
        ]
    )
    provider = CrashingProvider()
    coordinator = DispatchCoordinator(
        repository,
        directory,
        provider,
        policy=EscalationPolicy(
            provider_submission_timeout=timedelta(seconds=30)
        ),
        clock=clock,
    )
    case = _authorize(coordinator)

    with pytest.raises(WorkerCrash):
        coordinator.dispatch_next(TENANT_A, case.case_id)
    clock.advance(30)

    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    stored_case = repository.get_case(TENANT_A, case.case_id)
    stored_attempt = repository.list_attempts(TENANT_A, case.case_id)[0]
    assert stored_case.status is DispatchStatus.MANUAL_FOLLOW_UP
    assert stored_attempt.status is CallAttemptStatus.MANUAL_FOLLOW_UP
    assert stored_attempt.safe_error_code == "voice_submission_claim_expired"
    assert provider.invocations == 1


def test_provider_submission_claim_rejects_the_wrong_owner_token() -> None:
    class WorkerCrash(BaseException):
        pass

    class CrashingProvider:
        def place_call(self, request):
            del request
            raise WorkerCrash()

        def cancel_call(self, provider_call_reference: str) -> None:
            del provider_call_reference

    clock = Clock()
    repository = InMemoryDispatchRepository()
    coordinator = DispatchCoordinator(
        repository,
        InMemoryContactDirectory(
            [
                _contact("primary-a", ContactRole.PRIMARY),
                _contact("supervisor-a", ContactRole.SUPERVISOR),
            ]
        ),
        CrashingProvider(),
        clock=clock,
        claim_factory=lambda: "correct-claim-owner",
    )
    authorized = _authorize(coordinator)
    with pytest.raises(WorkerCrash):
        coordinator.dispatch_next(TENANT_A, authorized.case_id)
    case = repository.get_case(TENANT_A, authorized.case_id)
    attempt = repository.list_attempts(TENANT_A, authorized.case_id)[0]
    updated_case = replace(
        case,
        status=DispatchStatus.MANUAL_FOLLOW_UP,
        next_attempt_at=None,
        final_outcome=DispatchStatus.MANUAL_FOLLOW_UP.value,
        closed_at=clock(),
        updated_at=clock(),
        version=case.version + 1,
    )
    updated_attempt = replace(
        attempt,
        status=CallAttemptStatus.MANUAL_FOLLOW_UP,
        outcome="callback_requested",
        completed_at=clock(),
        safe_error_code="voice_submission_uncertain",
        updated_at=clock(),
        version=attempt.version + 1,
    )
    event = DispatchEvent(
        event_id="event-owner-mismatch",
        tenant_id=TENANT_A,
        case_id=case.case_id,
        attempt_id=attempt.attempt_id,
        kind=DispatchEventKind.MANUAL_FOLLOW_UP,
        occurred_at=clock(),
        deduplication_key=hashlib.sha256(b"owner-mismatch").hexdigest(),
        detail_code="voice_submission_uncertain",
    )

    with pytest.raises(DispatchStateConflict):
        repository.finish_provider_submission(
            case=updated_case,
            attempt=updated_attempt,
            event=event,
            claim_token="wrong-claim-owner",
            outcome="uncertain",
            expected_case_version=case.version,
            expected_attempt_version=attempt.version,
        )

    assert repository.get_case(TENANT_A, case.case_id) == case
    assert repository.list_attempts(TENANT_A, case.case_id)[0] == attempt


def test_uncertain_provider_result_is_never_redialed() -> None:
    class UncertainProvider:
        def __init__(self) -> None:
            self.invocations = 0

        def place_call(self, request):
            del request
            self.invocations += 1
            raise VoiceSubmissionUncertain()

        def cancel_call(self, provider_call_reference: str) -> None:
            del provider_call_reference

    clock = Clock()
    repository = InMemoryDispatchRepository()
    provider = UncertainProvider()
    coordinator = DispatchCoordinator(
        repository,
        InMemoryContactDirectory(
            [
                _contact("primary-a", ContactRole.PRIMARY),
                _contact("supervisor-a", ContactRole.SUPERVISOR),
            ]
        ),
        provider,
        clock=clock,
    )
    case = _authorize(coordinator)

    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    assert repository.get_case(TENANT_A, case.case_id).status is DispatchStatus.MANUAL_FOLLOW_UP
    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    assert provider.invocations == 1


def test_delivery_exhaustion_is_terminal_visible_and_idempotent() -> None:
    coordinator, repository, provider, _ = _system(failures=1)
    case = _authorize(coordinator)

    with pytest.raises(VoiceProviderUnavailable):
        coordinator.dispatch_next(TENANT_A, case.case_id)

    exhausted = coordinator.mark_delivery_exhausted(TENANT_A, case.case_id)
    duplicate = coordinator.mark_delivery_exhausted(TENANT_A, case.case_id)
    stored_attempt = repository.list_attempts(TENANT_A, case.case_id)[0]

    assert exhausted.status is DispatchStatus.MANUAL_FOLLOW_UP
    assert duplicate.status is DispatchStatus.MANUAL_FOLLOW_UP
    assert stored_attempt.status is CallAttemptStatus.MANUAL_FOLLOW_UP
    assert stored_attempt.outcome == "failed"
    assert stored_attempt.safe_error_code == "dispatch_delivery_exhausted"
    assert repository.list_events(TENANT_A, case.case_id)[-1].detail_code == (
        "dispatch_delivery_exhausted"
    )
    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    assert provider.place_call_invocations == 1


def test_delivery_exhaustion_before_an_attempt_closes_the_case() -> None:
    coordinator, repository, _, _ = _system()
    case = _authorize(coordinator)

    exhausted = coordinator.mark_delivery_exhausted(TENANT_A, case.case_id)

    assert exhausted.status is DispatchStatus.MANUAL_FOLLOW_UP
    assert exhausted.final_outcome == DispatchStatus.MANUAL_FOLLOW_UP.value
    assert repository.list_attempts(TENANT_A, case.case_id) == ()


def test_callback_token_factory_receives_attempt_id_for_restart_recovery() -> None:
    clock = Clock()
    repository = InMemoryDispatchRepository()
    received_attempt_ids: list[str] = []

    def token_for(attempt_id: str) -> str:
        received_attempt_ids.append(attempt_id)
        return "r" * 43

    coordinator = DispatchCoordinator(
        repository,
        InMemoryContactDirectory(
            [
                _contact("primary-a", ContactRole.PRIMARY),
                _contact("supervisor-a", ContactRole.SUPERVISOR),
            ]
        ),
        MockTwilioVoiceProvider(),
        clock=clock,
        token_factory=token_for,
    )
    case = _authorize(coordinator)

    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)

    assert attempt is not None
    assert received_attempt_ids == [attempt.attempt_id]
    assert attempt.callback_token == "r" * 43


def test_authorization_persists_the_requested_template_and_policy_snapshot() -> None:
    coordinator, _, _, _ = _system()
    case = coordinator.authorize(
        _incident(),
        authorized_by="reviewer-subject-a",
        idempotency_key="dispatch-template-0001",
        authorize_call=True,
        message_template_version="dispatch-alert-v2",
    )

    assert case.message_template_version == "dispatch-alert-v2"
    assert case.policy_version == "dispatch-escalation-v1"
    assert case.retry_delay_seconds == 30
    assert case.attempt_count == 0


def test_unknown_answering_machine_result_is_not_treated_as_human() -> None:
    coordinator, _, _, clock = _system(retry_seconds=5)
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None

    updated = coordinator.handle_answering_machine(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        result="unknown",
        event_key="amd-1",
    )

    assert updated.status is DispatchStatus.RETRY_SCHEDULED
    clock.advance(5)
    assert coordinator.dispatch_next(TENANT_A, case.case_id).sequence == 2


def test_cancel_stops_a_live_attempt_and_is_idempotent() -> None:
    coordinator, _, provider, _ = _system()
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None

    canceled = coordinator.cancel(
        TENANT_A,
        case.case_id,
        canceled_by="reviewer-subject-a",
        event_key="cancel-1",
    )
    duplicate = coordinator.cancel(
        TENANT_A,
        case.case_id,
        canceled_by="reviewer-subject-a",
        event_key="cancel-1",
    )

    assert canceled.status is DispatchStatus.CANCELED
    assert duplicate.status is DispatchStatus.CANCELED
    assert attempt.provider_call_reference in provider.canceled_references


def test_callback_mapping_and_repository_access_are_tenant_safe() -> None:
    coordinator, repository, _, _ = _system()
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None

    with pytest.raises(WebhookCallMismatch):
        coordinator.handle_status(
            attempt.callback_token,
            provider_call_reference="wrong-call",
            status="completed",
            event_key="forged",
        )
    with pytest.raises(DispatchResourceNotFound):
        repository.get_case(TENANT_B, case.case_id)
    with pytest.raises(DispatchResourceNotFound):
        repository.list_attempts(TENANT_B, case.case_id)


def test_callback_accepts_a_constant_time_sha256_provider_reference_mapping() -> None:
    class HashingCallbackRepository(InMemoryDispatchRepository):
        def resolve_callback(self, callback_token: str):
            case, attempt = super().resolve_callback(callback_token)
            if (
                attempt.provider_call_reference
                and not attempt.provider_call_reference.startswith("sha256:")
            ):
                digest = hashlib.sha256(
                    attempt.provider_call_reference.encode("utf-8")
                ).hexdigest()
                attempt = replace(attempt, provider_call_reference=f"sha256:{digest}")
            return case, attempt

    clock = Clock()
    repository = HashingCallbackRepository()
    provider = MockTwilioVoiceProvider()
    coordinator = DispatchCoordinator(
        repository,
        InMemoryContactDirectory(
            [
                _contact("primary-a", ContactRole.PRIMARY),
                _contact("supervisor-a", ContactRole.SUPERVISOR),
            ]
        ),
        provider,
        clock=clock,
    )
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None

    updated = coordinator.handle_status(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        status="ringing",
        event_key="hash-only-routing-1",
    )
    assert updated.status is DispatchStatus.DIALING

    with pytest.raises(WebhookCallMismatch):
        coordinator.handle_status(
            attempt.callback_token,
            provider_call_reference="CA-forged-call",
            status="completed",
            event_key="hash-only-routing-forged",
        )


def test_same_idempotency_key_cannot_be_reused_with_a_different_principal() -> None:
    coordinator, _, _, _ = _system()
    _authorize(coordinator)
    with pytest.raises(DispatchIdempotencyConflict):
        coordinator.authorize(
            _incident(),
            authorized_by="another-reviewer",
            idempotency_key="dispatch-key-0001",
            authorize_call=True,
        )


def test_late_acknowledgement_can_stop_a_scheduled_retry() -> None:
    coordinator, repository, provider, clock = _system()
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None
    _complete(coordinator, attempt, event_key="completed-1")

    acknowledged = coordinator.handle_gather(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        digits="1",
        event_key="late-gather-1",
    )
    clock.advance(60)

    assert acknowledged.status is DispatchStatus.ACKNOWLEDGED
    assert coordinator.dispatch_next(TENANT_A, case.case_id) is None
    assert len(provider.requests) == 1
    assert (
        repository.list_attempts(TENANT_A, case.case_id)[0].status
        is CallAttemptStatus.ACKNOWLEDGED
    )


def test_confirmed_incident_repr_does_not_include_location_or_category() -> None:
    incident = _incident()
    rendered = repr(incident)
    assert incident.broad_location_label not in rendered
    assert incident.category not in rendered

    coordinator, _, _, _ = _system()
    case = _authorize(coordinator)
    case_rendered = repr(case)
    assert case.authorized_by not in case_rendered
    assert case.broad_location_label not in case_rendered
    assert case.authorization_fingerprint not in case_rendered


def test_out_of_order_provider_progress_does_not_regress_attempt_state() -> None:
    coordinator, repository, _, _ = _system()
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None
    coordinator.handle_status(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        status="ringing",
        event_key="ringing-1",
    )
    coordinator.handle_status(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        status="initiated",
        event_key="late-initiated-1",
    )
    assert (
        repository.list_attempts(TENANT_A, case.case_id)[0].status
        is CallAttemptStatus.RINGING
    )


def test_status_webhook_is_idempotent_by_call_reference_event_type_and_status() -> None:
    coordinator, repository, _, _ = _system()
    case = _authorize(coordinator)
    attempt = coordinator.dispatch_next(TENANT_A, case.case_id)
    assert attempt is not None

    first = coordinator.handle_status(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        status="ringing",
        event_key="delivery-1",
    )
    event_count = len(repository.list_events(TENANT_A, case.case_id))
    duplicate = coordinator.handle_status(
        attempt.callback_token,
        provider_call_reference=attempt.provider_call_reference,
        status="ringing",
        event_key="provider-redelivery-with-different-request-id",
    )

    assert duplicate.version == first.version
    assert len(repository.list_events(TENANT_A, case.case_id)) == event_count
