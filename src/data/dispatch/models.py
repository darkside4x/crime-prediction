"""Tenant-scoped domain records for human-authorized voice dispatch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import DispatchValidationError

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CASE_REFERENCE = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,31}$")
_BROAD_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &'()/-]{0,119}$")
_ZONE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_H3_CELL = re.compile(r"^[0-9a-f]{15}$")
_SECRET_REFERENCE = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9_./:\-]{0,511}$")


def require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DispatchValidationError(
            "dispatch_timestamp_invalid", f"{field_name} must include a timezone"
        )
    return value.astimezone(UTC)


def require_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise DispatchValidationError(
            "dispatch_identifier_invalid", f"{field_name} is invalid"
        )
    return value


class ContactRole(StrEnum):
    PRIMARY = "primary"
    SUPERVISOR = "supervisor"


class DispatchStatus(StrEnum):
    QUEUED = "queued"
    DIALING = "dialing"
    AWAITING_ACKNOWLEDGEMENT = "awaiting_acknowledgement"
    PROVIDER_RETRY = "provider_retry"
    RETRY_SCHEDULED = "retry_scheduled"
    SUPERVISOR_SCHEDULED = "supervisor_scheduled"
    ACKNOWLEDGED = "acknowledged"
    MANUAL_FOLLOW_UP = "manual_follow_up"
    UNACKNOWLEDGED = "unacknowledged"
    CANCELED = "canceled"


TERMINAL_DISPATCH_STATUSES = frozenset(
    {
        DispatchStatus.ACKNOWLEDGED,
        DispatchStatus.MANUAL_FOLLOW_UP,
        DispatchStatus.UNACKNOWLEDGED,
        DispatchStatus.CANCELED,
    }
)


class CallAttemptStatus(StrEnum):
    RESERVED = "reserved"
    PROVIDER_RETRY = "provider_retry"
    INITIATED = "initiated"
    RINGING = "ringing"
    ANSWERED = "answered"
    ACKNOWLEDGED = "acknowledged"
    MANUAL_FOLLOW_UP = "manual_follow_up"
    UNACKNOWLEDGED = "unacknowledged"
    CANCELED = "canceled"


TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        CallAttemptStatus.ACKNOWLEDGED,
        CallAttemptStatus.MANUAL_FOLLOW_UP,
        CallAttemptStatus.UNACKNOWLEDGED,
        CallAttemptStatus.CANCELED,
    }
)


class DispatchEventKind(StrEnum):
    AUTHORIZED = "authorized"
    ATTEMPT_RESERVED = "attempt_reserved"
    CALL_INITIATED = "call_initiated"
    PROVIDER_RETRY = "provider_retry"
    PROVIDER_STATUS = "provider_status"
    ANSWERING_MACHINE = "answering_machine"
    ACKNOWLEDGED = "acknowledged"
    MANUAL_FOLLOW_UP = "manual_follow_up"
    INVALID_GATHER_INPUT = "invalid_gather_input"
    RETRY_SCHEDULED = "retry_scheduled"
    SUPERVISOR_SCHEDULED = "supervisor_scheduled"
    EXHAUSTED = "exhausted"
    CANCELED = "canceled"


@dataclass(frozen=True)
class CallingWindow:
    """A contact's local weekly availability, including overnight windows."""

    weekdays: frozenset[int]
    start: time
    end: time

    def __post_init__(self) -> None:
        if not self.weekdays or not self.weekdays <= frozenset(range(7)):
            raise DispatchValidationError(
                "dispatch_calling_window_invalid", "Calling weekdays are invalid"
            )
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise DispatchValidationError(
                "dispatch_calling_window_invalid",
                "Calling-window times must be local wall-clock values",
            )
        if self.start == self.end:
            raise DispatchValidationError(
                "dispatch_calling_window_invalid", "Calling window cannot be empty"
            )

    def contains(self, local_value: datetime) -> bool:
        local_time = local_value.timetz().replace(tzinfo=None)
        weekday = local_value.weekday()
        if self.start < self.end:
            return weekday in self.weekdays and self.start <= local_time < self.end
        return (weekday in self.weekdays and local_time >= self.start) or (
            (weekday - 1) % 7 in self.weekdays and local_time < self.end
        )


@dataclass(frozen=True, repr=False)
class ResponseContact:
    contact_id: str
    tenant_id: str
    zone_id: str
    role: ContactRole
    phone_secret_ref: str
    display_name: str
    enabled: bool = True
    opted_in_at: datetime | None = None
    verified_at: datetime | None = None
    coverage_cells: frozenset[str] = frozenset()
    timezone: str = "UTC"
    calling_windows: tuple[CallingWindow, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.contact_id, "contact_id")
        require_identifier(self.tenant_id, "tenant_id")
        if not _ZONE_ID.fullmatch(self.zone_id):
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Contact zone is invalid"
            )
        if not isinstance(self.role, ContactRole):
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Contact role is invalid"
            )
        if not _SECRET_REFERENCE.fullmatch(self.phone_secret_ref):
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Contact phone reference is invalid"
            )
        if not self.display_name or len(self.display_name) > 120:
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Contact display name is invalid"
            )
        if any(not _H3_CELL.fullmatch(cell) for cell in self.coverage_cells):
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Contact coverage is invalid"
            )
        if self.opted_in_at is not None:
            require_utc(self.opted_in_at, "opted_in_at")
        if self.verified_at is not None:
            require_utc(self.verified_at, "verified_at")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Contact timezone is invalid"
            ) from error

    def is_available(self, at: datetime) -> bool:
        when = require_utc(at, "availability time")
        if (
            not self.enabled
            or self.opted_in_at is None
            or self.verified_at is None
            or require_utc(self.opted_in_at, "opted_in_at") > when
            or require_utc(self.verified_at, "verified_at") > when
        ):
            return False
        value = when.astimezone(ZoneInfo(self.timezone))
        return not self.calling_windows or any(
            window.contains(value) for window in self.calling_windows
        )

    def __repr__(self) -> str:
        return (
            "ResponseContact("
            f"contact_id={self.contact_id!r}, tenant_id={self.tenant_id!r}, "
            f"zone_id={self.zone_id!r}, role={self.role.value!r}, enabled={self.enabled!r}, "
            f"opted_in={self.opted_in_at is not None!r})"
        )


@dataclass(frozen=True)
class CoverageTarget:
    zone_id: str
    cell_id: str

    def __post_init__(self) -> None:
        if not _ZONE_ID.fullmatch(self.zone_id) or not _H3_CELL.fullmatch(self.cell_id):
            raise DispatchValidationError(
                "dispatch_coverage_invalid", "Dispatch coverage is invalid"
            )


@dataclass(frozen=True)
class ResolvedContacts:
    primary: ResponseContact
    supervisor: ResponseContact

    def __post_init__(self) -> None:
        if self.primary.tenant_id != self.supervisor.tenant_id:
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Escalation contacts must share a tenant"
            )
        if self.primary.role is not ContactRole.PRIMARY:
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Primary escalation contact is invalid"
            )
        if self.supervisor.role is not ContactRole.SUPERVISOR:
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Supervisor escalation contact is invalid"
            )
        if self.primary.zone_id != self.supervisor.zone_id:
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Escalation contacts must share a zone"
            )


@dataclass(frozen=True, repr=False)
class ConfirmedIncidentRef:
    tenant_id: str
    incident_id: str
    confirmed_review_id: str
    incident_source_id: str
    incident_external_event_id: str
    cell_id: str
    zone_id: str
    case_reference: str
    category: str
    broad_location_label: str
    occurred_at: datetime
    review_decision: str = "confirmed"

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "incident_id",
            "confirmed_review_id",
            "incident_source_id",
        ):
            require_identifier(getattr(self, name), name)
        CoverageTarget(zone_id=self.zone_id, cell_id=self.cell_id)
        if (
            not self.incident_external_event_id
            or len(self.incident_external_event_id) > 256
        ):
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Incident event reference is invalid"
            )
        if not _CASE_REFERENCE.fullmatch(self.case_reference):
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Case reference is invalid"
            )
        if self.category not in {
            "property",
            "violence",
            "public_order",
            "traffic_safety",
            "other",
        }:
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Confirmed category is invalid"
            )
        if not _BROAD_LABEL.fullmatch(self.broad_location_label):
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Broad location label is invalid"
            )
        require_utc(self.occurred_at, "occurred_at")
        if self.review_decision not in {"confirmed", "rejected"}:
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Review decision is invalid"
            )

    @property
    def coverage(self) -> CoverageTarget:
        return CoverageTarget(zone_id=self.zone_id, cell_id=self.cell_id)

    def __repr__(self) -> str:
        return (
            "ConfirmedIncidentRef("
            f"tenant_id={self.tenant_id!r}, incident_id={self.incident_id!r}, "
            f"confirmed_review_id={self.confirmed_review_id!r}, "
            f"review_decision={self.review_decision!r})"
        )


@dataclass(frozen=True, repr=False)
class DispatchCase:
    case_id: str
    tenant_id: str
    incident_id: str
    confirmed_review_id: str
    incident_source_id: str
    incident_external_event_id: str
    zone_id: str
    primary_contact_id: str
    supervisor_contact_id: str
    case_reference: str
    category: str
    broad_location_label: str
    occurred_at: datetime
    authorized_by: str
    authorization_fingerprint: str
    idempotency_key_hash: str
    policy_version: str
    message_template_version: str
    retry_delay_seconds: int
    status: DispatchStatus
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None
    attempt_count: int
    final_outcome: str | None
    closed_at: datetime | None
    version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "case_id",
            "tenant_id",
            "incident_id",
            "confirmed_review_id",
            "incident_source_id",
            "primary_contact_id",
            "supervisor_contact_id",
        ):
            require_identifier(getattr(self, name), name)
        if not _ZONE_ID.fullmatch(self.zone_id):
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Dispatch zone is invalid"
            )
        if (
            not self.incident_external_event_id
            or len(self.incident_external_event_id) > 256
        ):
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Incident event reference is invalid"
            )
        if self.primary_contact_id == self.supervisor_contact_id:
            raise DispatchValidationError(
                "dispatch_contact_invalid", "Escalation contacts must be distinct"
            )
        if not _CASE_REFERENCE.fullmatch(self.case_reference):
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Case reference is invalid"
            )
        if self.category not in {
            "property",
            "violence",
            "public_order",
            "traffic_safety",
            "other",
        }:
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Confirmed category is invalid"
            )
        if not _BROAD_LABEL.fullmatch(self.broad_location_label):
            raise DispatchValidationError(
                "dispatch_incident_invalid", "Broad location label is invalid"
            )
        if not self.authorized_by or len(self.authorized_by) > 128:
            raise DispatchValidationError(
                "dispatch_authorization_invalid", "Authorizing principal is invalid"
            )
        if not re.fullmatch(r"[a-f0-9]{64}", self.authorization_fingerprint):
            raise DispatchValidationError(
                "dispatch_authorization_invalid", "Authorization fingerprint is invalid"
            )
        if not re.fullmatch(r"[a-f0-9]{64}", self.idempotency_key_hash):
            raise DispatchValidationError(
                "dispatch_authorization_invalid", "Idempotency key hash is invalid"
            )
        require_identifier(self.policy_version, "policy_version")
        require_identifier(self.message_template_version, "message_template_version")
        if not 5 <= self.retry_delay_seconds <= 3600:
            raise DispatchValidationError(
                "dispatch_policy_invalid", "Retry delay is invalid"
            )
        require_utc(self.occurred_at, "occurred_at")
        require_utc(self.created_at, "created_at")
        require_utc(self.updated_at, "updated_at")
        if self.next_attempt_at is not None:
            require_utc(self.next_attempt_at, "next_attempt_at")
        if self.closed_at is not None:
            require_utc(self.closed_at, "closed_at")
        if not 0 <= self.attempt_count <= 3:
            raise DispatchValidationError(
                "dispatch_attempt_invalid", "Attempt count is invalid"
            )
        if self.status in TERMINAL_DISPATCH_STATUSES:
            if (
                self.final_outcome != self.status.value
                or self.closed_at is None
                or self.next_attempt_at is not None
            ):
                raise DispatchValidationError(
                    "dispatch_state_invalid", "Terminal dispatch state is incomplete"
                )
        elif self.final_outcome is not None or self.closed_at is not None:
            raise DispatchValidationError(
                "dispatch_state_invalid", "Open dispatch state has a final outcome"
            )
        if self.version < 1:
            raise DispatchValidationError(
                "dispatch_version_invalid", "Dispatch version is invalid"
            )

    def __repr__(self) -> str:
        return (
            "DispatchCase("
            f"case_id={self.case_id!r}, tenant_id={self.tenant_id!r}, "
            f"incident_id={self.incident_id!r}, status={self.status.value!r}, "
            f"version={self.version!r})"
        )


@dataclass(frozen=True, repr=False)
class CallAttempt:
    attempt_id: str
    case_id: str
    tenant_id: str
    sequence: int
    target_role: ContactRole
    contact_id: str
    callback_token: str
    provider_call_reference: str | None
    status: CallAttemptStatus
    created_at: datetime
    updated_at: datetime
    outcome: str | None = None
    initiated_at: datetime | None = None
    answered_at: datetime | None = None
    completed_at: datetime | None = None
    next_action_at: datetime | None = None
    safe_error_code: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        for name in ("attempt_id", "case_id", "tenant_id", "contact_id"):
            require_identifier(getattr(self, name), name)
        if self.sequence not in {1, 2, 3}:
            raise DispatchValidationError(
                "dispatch_attempt_invalid", "Call-attempt sequence is invalid"
            )
        expected_role = (
            ContactRole.PRIMARY if self.sequence <= 2 else ContactRole.SUPERVISOR
        )
        if self.target_role is not expected_role:
            raise DispatchValidationError(
                "dispatch_attempt_invalid", "Call-attempt escalation role is invalid"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.callback_token):
            raise DispatchValidationError(
                "dispatch_attempt_invalid", "Callback token is invalid"
            )
        if (
            self.provider_call_reference is not None
            and not self.provider_call_reference
        ):
            raise DispatchValidationError(
                "dispatch_attempt_invalid", "Provider call reference is invalid"
            )
        require_utc(self.created_at, "created_at")
        require_utc(self.updated_at, "updated_at")
        for name in (
            "initiated_at",
            "answered_at",
            "completed_at",
            "next_action_at",
        ):
            value = getattr(self, name)
            if value is not None:
                require_utc(value, name)
        if self.outcome not in {
            None,
            "acknowledged",
            "callback_requested",
            "no_answer",
            "busy",
            "failed",
            "no_acknowledgement",
            "canceled",
        }:
            raise DispatchValidationError(
                "dispatch_attempt_invalid", "Call-attempt outcome is invalid"
            )
        if self.safe_error_code is not None and not re.fullmatch(
            r"[a-z0-9_]{1,80}", self.safe_error_code
        ):
            raise DispatchValidationError(
                "dispatch_attempt_invalid", "Call-attempt error code is invalid"
            )
        if self.version < 1:
            raise DispatchValidationError(
                "dispatch_version_invalid", "Attempt version is invalid"
            )

    def __repr__(self) -> str:
        return (
            "CallAttempt("
            f"attempt_id={self.attempt_id!r}, case_id={self.case_id!r}, "
            f"tenant_id={self.tenant_id!r}, sequence={self.sequence!r}, "
            f"target_role={self.target_role.value!r}, status={self.status.value!r}, "
            f"version={self.version!r})"
        )


@dataclass(frozen=True, repr=False)
class DispatchEvent:
    event_id: str
    tenant_id: str
    case_id: str
    attempt_id: str | None
    kind: DispatchEventKind
    occurred_at: datetime
    deduplication_key: str
    detail_code: str | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "tenant_id", "case_id"):
            require_identifier(getattr(self, name), name)
        if self.attempt_id is not None:
            require_identifier(self.attempt_id, "attempt_id")
        require_utc(self.occurred_at, "occurred_at")
        if not re.fullmatch(r"[a-f0-9]{64}", self.deduplication_key):
            raise DispatchValidationError(
                "dispatch_event_invalid", "Event deduplication key is invalid"
            )
        if self.detail_code is not None:
            require_identifier(self.detail_code, "detail_code")

    def __repr__(self) -> str:
        return (
            "DispatchEvent("
            f"event_id={self.event_id!r}, tenant_id={self.tenant_id!r}, "
            f"case_id={self.case_id!r}, kind={self.kind.value!r})"
        )
