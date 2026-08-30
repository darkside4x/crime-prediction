"""PostgreSQL/RLS adapters for response contacts and durable dispatch state."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.data.postgres import TenantPostgres

from .directory import ContactDirectory
from .errors import (
    DispatchContactUnavailable,
    DispatchIdempotencyConflict,
    DispatchResourceNotFound,
    DispatchRetryNotDue,
    DispatchStateConflict,
)
from .models import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_DISPATCH_STATUSES,
    CallAttempt,
    CallAttemptStatus,
    CallingWindow,
    ContactRole,
    CoverageTarget,
    DispatchCase,
    DispatchEvent,
    DispatchEventKind,
    DispatchStatus,
    ResolvedContacts,
    ResponseContact,
)
from .repository import (
    AttemptReservation,
    DispatchRepository,
    ProviderSubmissionClaim,
)
from .secrets import HmacCallbackTokenCodec


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Database timestamp must include a timezone")
    return value.astimezone(UTC)


class PostgresContactDirectory(ContactDirectory):
    development_only = False

    def __init__(self, database: TenantPostgres) -> None:
        self.database = database

    @staticmethod
    def from_row(row: Any) -> ResponseContact:
        window = CallingWindow(
            weekdays=frozenset(int(value) for value in row["calling_days"]),
            start=row["calling_window_start"],
            end=row["calling_window_end"],
        )
        return ResponseContact(
            contact_id=str(row["contact_id"]),
            tenant_id=str(row["tenant_id"]),
            zone_id=row["zone_id"],
            role=ContactRole(row["role"]),
            phone_secret_ref=row["destination_secret_ref"],
            display_name=row["contact_label"],
            enabled=bool(row["enabled"]),
            opted_in_at=row["opted_in_at"],
            verified_at=row["verified_at"],
            coverage_cells=frozenset(row["coverage_h3_cells"]),
            timezone=row["timezone"],
            calling_windows=(window,),
        )

    def get_contact(self, tenant_id: str, contact_id: str) -> ResponseContact:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                "SELECT * FROM response_contacts WHERE tenant_id=%s AND contact_id=%s",
                (tenant_id, contact_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise DispatchResourceNotFound()
        return self.from_row(row)

    def resolve(
        self,
        tenant_id: str,
        coverage: CoverageTarget,
        *,
        at: datetime,
    ) -> ResolvedContacts:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM response_contacts
                   WHERE tenant_id=%s AND zone_id=%s AND enabled
                     AND opted_in_at IS NOT NULL
                     AND %s = ANY(coverage_h3_cells)
                   ORDER BY role, contact_id""",
                (tenant_id, coverage.zone_id, coverage.cell_id),
            )
            contacts = [self.from_row(row) for row in cursor.fetchall()]
        available = [contact for contact in contacts if contact.is_available(at)]
        primary = [item for item in available if item.role is ContactRole.PRIMARY]
        supervisor = [item for item in available if item.role is ContactRole.SUPERVISOR]
        if len(primary) > 1 or len(supervisor) > 1:
            raise DispatchContactUnavailable(ambiguous=True)
        if len(primary) != 1 or len(supervisor) != 1:
            raise DispatchContactUnavailable()
        return ResolvedContacts(primary=primary[0], supervisor=supervisor[0])


class PostgresDispatchRepository(DispatchRepository):
    development_only = False

    def __init__(
        self,
        database: TenantPostgres,
        *,
        callback_tokens: HmacCallbackTokenCodec,
        callback_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self.database = database
        self.callback_tokens = callback_tokens
        self.callback_ttl = callback_ttl

    @staticmethod
    def _case(row: Any) -> DispatchCase:
        return DispatchCase(
            case_id=str(row["dispatch_case_id"]),
            tenant_id=str(row["tenant_id"]),
            incident_id=str(row["incident_id"]),
            confirmed_review_id=str(row["review_id"]),
            incident_source_id=str(row["incident_source_id"]),
            incident_external_event_id=row["incident_external_event_id"],
            zone_id=row["zone_id"],
            primary_contact_id=str(row["primary_contact_id"]),
            supervisor_contact_id=str(row["supervisor_contact_id"]),
            case_reference=row["case_reference"],
            category=row["confirmed_category"],
            broad_location_label=row["broad_location_label"],
            occurred_at=_utc(row["occurred_at"]),
            authorized_by=row["authorized_by"],
            authorization_fingerprint=row["authorization_fingerprint"],
            idempotency_key_hash=row["idempotency_key_hash"],
            policy_version=row["policy_version"],
            message_template_version=row["message_template_version"],
            retry_delay_seconds=int(row["retry_delay_seconds"]),
            status=DispatchStatus(row["state"]),
            created_at=_utc(row["created_at"]),
            updated_at=_utc(row["updated_at"]),
            next_attempt_at=(
                None if row["next_attempt_at"] is None else _utc(row["next_attempt_at"])
            ),
            attempt_count=int(row["attempt_count"]),
            final_outcome=row["final_outcome"],
            closed_at=None if row["closed_at"] is None else _utc(row["closed_at"]),
            version=int(row["version"]),
        )

    def _attempt(self, row: Any) -> CallAttempt:
        provider_hash = row["provider_call_id_hash"]
        return CallAttempt(
            attempt_id=str(row["attempt_id"]),
            case_id=str(row["dispatch_case_id"]),
            tenant_id=str(row["tenant_id"]),
            sequence=int(row["attempt_number"]),
            target_role=ContactRole(row["recipient_role"]),
            contact_id=str(row["contact_id"]),
            callback_token=self.callback_tokens.encode(str(row["attempt_id"])),
            provider_call_reference=(
                None if provider_hash is None else f"sha256:{provider_hash}"
            ),
            status=CallAttemptStatus(row["state"]),
            created_at=_utc(row["created_at"]),
            updated_at=_utc(row["updated_at"]),
            outcome=row["outcome"],
            initiated_at=(
                None if row["initiated_at"] is None else _utc(row["initiated_at"])
            ),
            answered_at=(
                None if row["answered_at"] is None else _utc(row["answered_at"])
            ),
            completed_at=(
                None if row["completed_at"] is None else _utc(row["completed_at"])
            ),
            next_action_at=(
                None if row["next_action_at"] is None else _utc(row["next_action_at"])
            ),
            safe_error_code=row["safe_error_code"],
            version=int(row["version"]),
        )

    @staticmethod
    def _actor(event: DispatchEvent) -> str:
        kind = event.kind
        if kind is DispatchEventKind.AUTHORIZED:
            return "reviewer"
        if kind in {
            DispatchEventKind.PROVIDER_STATUS,
            DispatchEventKind.ANSWERING_MACHINE,
        }:
            return "provider"
        if kind is DispatchEventKind.MANUAL_FOLLOW_UP and (
            (event.detail_code or "").startswith("voice_submission_")
            or (event.detail_code or "").startswith("dispatch_delivery_")
        ):
            return "system"
        if kind in {
            DispatchEventKind.ACKNOWLEDGED,
            DispatchEventKind.MANUAL_FOLLOW_UP,
            DispatchEventKind.INVALID_GATHER_INPUT,
        }:
            return "contact"
        return "system"

    def _insert_event(
        self,
        cursor: Any,
        event: DispatchEvent,
        *,
        attempt: CallAttempt | None,
    ) -> bool:
        cursor.execute(
            """INSERT INTO dispatch_events
               (tenant_id,event_id,dispatch_case_id,attempt_id,attempt_number,
                event_type,actor_type,recipient_role,safe_code,dedupe_key_hash,occurred_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id,dedupe_key_hash) DO NOTHING""",
            (
                event.tenant_id,
                event.event_id,
                event.case_id,
                event.attempt_id,
                None if attempt is None else attempt.sequence,
                event.kind.value,
                self._actor(event),
                None if attempt is None else attempt.target_role.value,
                event.detail_code,
                event.deduplication_key,
                event.occurred_at,
            ),
        )
        return cursor.rowcount == 1

    def create_case(
        self,
        case: DispatchCase,
        *,
        idempotency_key: str,
        event: DispatchEvent,
    ) -> tuple[DispatchCase, bool]:
        if _digest(idempotency_key) != case.idempotency_key_hash:
            raise DispatchIdempotencyConflict()
        with self.database.transaction(case.tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO dispatch_cases
                   (tenant_id,dispatch_case_id,incident_id,review_id,
                    incident_source_id,incident_external_event_id,case_reference,
                    confirmed_category,occurred_at,broad_location_label,zone_id,
                    primary_contact_id,supervisor_contact_id,call_authorized,
                    authorized_by,authorized_at,authorization_fingerprint,
                    idempotency_key_hash,policy_version,message_template_version,
                    maximum_attempts,retry_delay_seconds,state,attempt_count,
                    next_attempt_at,final_outcome,closed_at,created_at,updated_at,version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,
                           %s,%s,%s,%s,%s,%s,3,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id,idempotency_key_hash) DO NOTHING""",
                (
                    case.tenant_id,
                    case.case_id,
                    case.incident_id,
                    case.confirmed_review_id,
                    case.incident_source_id,
                    case.incident_external_event_id,
                    case.case_reference,
                    case.category,
                    case.occurred_at,
                    case.broad_location_label,
                    case.zone_id,
                    case.primary_contact_id,
                    case.supervisor_contact_id,
                    case.authorized_by,
                    case.created_at,
                    case.authorization_fingerprint,
                    case.idempotency_key_hash,
                    case.policy_version,
                    case.message_template_version,
                    case.retry_delay_seconds,
                    case.status.value,
                    case.attempt_count,
                    case.next_attempt_at,
                    case.final_outcome,
                    case.closed_at,
                    case.created_at,
                    case.updated_at,
                    case.version,
                ),
            )
            created = cursor.rowcount == 1
            if created:
                self._insert_event(cursor, event, attempt=None)
            cursor.execute(
                """SELECT * FROM dispatch_cases
                   WHERE tenant_id=%s AND idempotency_key_hash=%s""",
                (case.tenant_id, case.idempotency_key_hash),
            )
            row = cursor.fetchone()
        if row is None:
            raise DispatchStateConflict("dispatch_repository_conflict")
        return self._case(row), created

    def find_case_by_idempotency(
        self, tenant_id: str, idempotency_key: str
    ) -> DispatchCase | None:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM dispatch_cases
                   WHERE tenant_id=%s AND idempotency_key_hash=%s""",
                (tenant_id, _digest(idempotency_key)),
            )
            row = cursor.fetchone()
        return None if row is None else self._case(row)

    def get_case(self, tenant_id: str, case_id: str) -> DispatchCase:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                "SELECT * FROM dispatch_cases WHERE tenant_id=%s AND dispatch_case_id=%s",
                (tenant_id, case_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise DispatchResourceNotFound()
        return self._case(row)

    def list_attempts(self, tenant_id: str, case_id: str) -> tuple[CallAttempt, ...]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM dispatch_call_attempts
                   WHERE tenant_id=%s AND dispatch_case_id=%s
                   ORDER BY attempt_number""",
                (tenant_id, case_id),
            )
            rows = cursor.fetchall()
            if not rows:
                cursor.execute(
                    """SELECT 1 FROM dispatch_cases
                       WHERE tenant_id=%s AND dispatch_case_id=%s""",
                    (tenant_id, case_id),
                )
                if cursor.fetchone() is None:
                    raise DispatchResourceNotFound()
        return tuple(self._attempt(row) for row in rows)

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
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM dispatch_cases
                   WHERE tenant_id=%s AND dispatch_case_id=%s FOR UPDATE""",
                (tenant_id, case_id),
            )
            case_row = cursor.fetchone()
            if case_row is None:
                raise DispatchResourceNotFound()
            case = self._case(case_row)
            cursor.execute(
                """SELECT * FROM dispatch_call_attempts
                   WHERE tenant_id=%s AND dispatch_case_id=%s
                   ORDER BY attempt_number FOR UPDATE""",
                (tenant_id, case_id),
            )
            attempts = [self._attempt(row) for row in cursor.fetchall()]
            active = [
                attempt
                for attempt in attempts
                if attempt.status not in TERMINAL_ATTEMPT_STATUSES
            ]
            if active:
                return AttemptReservation(case=case, attempt=active[-1], created=False)
            if case.status in TERMINAL_DISPATCH_STATUSES:
                raise DispatchStateConflict("dispatch_case_terminal")
            if case.next_attempt_at is not None and at < case.next_attempt_at:
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
                case_id=case_id,
                tenant_id=tenant_id,
                sequence=sequence,
                target_role=role,
                contact_id=contact_id,
                callback_token=callback_token,
                provider_call_reference=None,
                status=CallAttemptStatus.RESERVED,
                created_at=at,
                updated_at=at,
                outcome=None,
                initiated_at=None,
                answered_at=None,
                completed_at=None,
                next_action_at=None,
                safe_error_code=None,
            )
            cursor.execute(
                """INSERT INTO dispatch_call_attempts
                   (tenant_id,attempt_id,dispatch_case_id,attempt_number,
                    recipient_role,contact_id,state,scheduled_at,
                    callback_token_hash,created_at,updated_at,version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""",
                (
                    tenant_id,
                    attempt_id,
                    case_id,
                    sequence,
                    role.value,
                    contact_id,
                    attempt.status.value,
                    at,
                    _digest(callback_token),
                    at,
                    at,
                ),
            )
            cursor.execute(
                """INSERT INTO dispatch_callback_routes
                   (callback_token_hash,tenant_id,attempt_id,created_at,expires_at)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    _digest(callback_token),
                    tenant_id,
                    attempt_id,
                    at,
                    at + self.callback_ttl,
                ),
            )
            cursor.execute(
                """UPDATE dispatch_cases SET state=%s,next_attempt_at=NULL,
                   attempt_count=%s,updated_at=%s,version=version+1
                   WHERE tenant_id=%s AND dispatch_case_id=%s AND version=%s
                   RETURNING *""",
                (
                    DispatchStatus.DIALING.value,
                    sequence,
                    at,
                    tenant_id,
                    case_id,
                    case.version,
                ),
            )
            updated_row = cursor.fetchone()
            if updated_row is None:
                raise DispatchStateConflict("dispatch_repository_conflict")
            updated_case = self._case(updated_row)
            if not self._insert_event(cursor, event, attempt=attempt):
                raise DispatchStateConflict("dispatch_repository_conflict")
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
        now = _utc(at)
        expires = _utc(deadline)
        if expires <= now or not claim_token:
            raise DispatchStateConflict("dispatch_submission_claim_invalid")
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM dispatch_cases
                   WHERE tenant_id=%s AND dispatch_case_id=%s FOR UPDATE""",
                (tenant_id, case_id),
            )
            case_row = cursor.fetchone()
            cursor.execute(
                """SELECT * FROM dispatch_call_attempts
                   WHERE tenant_id=%s AND attempt_id=%s
                     AND dispatch_case_id=%s FOR UPDATE""",
                (tenant_id, attempt_id, case_id),
            )
            attempt_row = cursor.fetchone()
            if case_row is None or attempt_row is None:
                raise DispatchResourceNotFound()
            case = self._case(case_row)
            attempt = self._attempt(attempt_row)
            submission_state = attempt_row["provider_submission_state"]
            raw_deadline = attempt_row["provider_submission_deadline"]
            if case.status in TERMINAL_DISPATCH_STATUSES:
                return ProviderSubmissionClaim(case=case, attempt=attempt, acquired=False)
            if submission_state == "claimed":
                retry_at = None if raw_deadline is None else _utc(raw_deadline)
                return ProviderSubmissionClaim(
                    case=case,
                    attempt=attempt,
                    acquired=False,
                    stale=retry_at is not None and retry_at <= now,
                    retry_at=retry_at,
                )
            if submission_state in {"submitted", "uncertain"}:
                return ProviderSubmissionClaim(case=case, attempt=attempt, acquired=False)
            if case.next_attempt_at is not None and now < case.next_attempt_at:
                raise DispatchRetryNotDue()
            if attempt.status not in {
                CallAttemptStatus.RESERVED,
                CallAttemptStatus.PROVIDER_RETRY,
            }:
                return ProviderSubmissionClaim(case=case, attempt=attempt, acquired=False)
            cursor.execute(
                """UPDATE dispatch_call_attempts
                   SET provider_submission_state='claimed',
                       provider_submission_owner_hash=%s,
                       provider_submission_claimed_at=%s,
                       provider_submission_deadline=%s,
                       provider_submission_completed_at=NULL,
                       updated_at=%s,version=version+1
                   WHERE tenant_id=%s AND attempt_id=%s AND version=%s
                     AND provider_submission_state='pending'
                   RETURNING *""",
                (
                    _digest(claim_token),
                    now,
                    expires,
                    now,
                    tenant_id,
                    attempt_id,
                    attempt.version,
                ),
            )
            claimed_attempt_row = cursor.fetchone()
            if claimed_attempt_row is None:
                raise DispatchStateConflict("dispatch_repository_conflict")
            cursor.execute(
                """UPDATE dispatch_cases
                   SET next_attempt_at=%s,updated_at=%s,version=version+1
                   WHERE tenant_id=%s AND dispatch_case_id=%s AND version=%s
                   RETURNING *""",
                (expires, now, tenant_id, case_id, case.version),
            )
            claimed_case_row = cursor.fetchone()
            if claimed_case_row is None:
                raise DispatchStateConflict("dispatch_repository_conflict")
        return ProviderSubmissionClaim(
            case=self._case(claimed_case_row),
            attempt=self._attempt(claimed_attempt_row),
            acquired=True,
            retry_at=expires,
        )

    @staticmethod
    def _submission_state_for(
        outcome: Literal["submitted", "retryable_failure", "uncertain"]
    ) -> str:
        return {
            "submitted": "submitted",
            "retryable_failure": "pending",
            "uncertain": "uncertain",
        }[outcome]

    def _update_claimed_attempt(
        self,
        cursor: Any,
        *,
        attempt: CallAttempt,
        expected_version: int,
        outcome: Literal["submitted", "retryable_failure", "uncertain"],
        claim_hash: str | None,
        expired_at: datetime | None = None,
    ) -> bool:
        submission_state = self._submission_state_for(outcome)
        clear_claim = outcome == "retryable_failure"
        cursor.execute(
            """UPDATE dispatch_call_attempts SET state=%s,outcome=%s,
               initiated_at=COALESCE(initiated_at,%s),
               answered_at=COALESCE(answered_at,%s),
               completed_at=COALESCE(completed_at,%s),next_action_at=%s,
               provider_call_id_hash=COALESCE(provider_call_id_hash,%s),
               safe_error_code=%s,updated_at=%s,version=%s,
               provider_submission_state=%s,
               provider_submission_owner_hash=CASE WHEN %s THEN NULL ELSE provider_submission_owner_hash END,
               provider_submission_claimed_at=CASE WHEN %s THEN NULL ELSE provider_submission_claimed_at END,
               provider_submission_deadline=CASE WHEN %s THEN NULL ELSE provider_submission_deadline END,
               provider_submission_completed_at=CASE WHEN %s THEN NULL ELSE %s END
               WHERE tenant_id=%s AND attempt_id=%s AND version=%s
                 AND provider_submission_state='claimed'
                 AND (%s::text IS NULL OR provider_submission_owner_hash=%s)
                 AND (%s::timestamptz IS NULL OR provider_submission_deadline<=%s)""",
            (
                attempt.status.value,
                self._outcome(attempt),
                attempt.initiated_at,
                attempt.answered_at,
                attempt.completed_at,
                attempt.next_action_at,
                self._provider_hash(attempt.provider_call_reference),
                attempt.safe_error_code,
                attempt.updated_at,
                attempt.version,
                submission_state,
                clear_claim,
                clear_claim,
                clear_claim,
                clear_claim,
                attempt.updated_at,
                attempt.tenant_id,
                attempt.attempt_id,
                expected_version,
                claim_hash,
                claim_hash,
                expired_at,
                expired_at,
            ),
        )
        return cursor.rowcount == 1

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
        with self.database.transaction(case.tenant_id) as cursor:
            cursor.execute(
                """SELECT 1 FROM dispatch_events
                   WHERE tenant_id=%s AND dedupe_key_hash=%s""",
                (case.tenant_id, event.deduplication_key),
            )
            if cursor.fetchone() is not None:
                return False
            if not self._update_case(cursor, case, expected_case_version):
                raise DispatchStateConflict("dispatch_repository_conflict")
            if not self._update_claimed_attempt(
                cursor,
                attempt=attempt,
                expected_version=expected_attempt_version,
                outcome=outcome,
                claim_hash=_digest(claim_token),
            ):
                raise DispatchStateConflict("dispatch_repository_conflict")
            if not self._insert_event(cursor, event, attempt=attempt):
                raise DispatchStateConflict("dispatch_repository_conflict")
        return True

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
        now = _utc(expired_at)
        with self.database.transaction(case.tenant_id) as cursor:
            cursor.execute(
                """SELECT 1 FROM dispatch_events
                   WHERE tenant_id=%s AND dedupe_key_hash=%s""",
                (case.tenant_id, event.deduplication_key),
            )
            if cursor.fetchone() is not None:
                return False
            if not self._update_case(cursor, case, expected_case_version):
                raise DispatchStateConflict("dispatch_repository_conflict")
            if not self._update_claimed_attempt(
                cursor,
                attempt=attempt,
                expected_version=expected_attempt_version,
                outcome="uncertain",
                claim_hash=None,
                expired_at=now,
            ):
                raise DispatchStateConflict("dispatch_repository_conflict")
            if not self._insert_event(cursor, event, attempt=attempt):
                raise DispatchStateConflict("dispatch_repository_conflict")
        return True

    @staticmethod
    def _provider_hash(reference: str | None) -> str | None:
        if reference is None:
            return None
        return (
            reference.removeprefix("sha256:")
            if reference.startswith("sha256:")
            else _digest(reference)
        )

    @staticmethod
    def _outcome(attempt: CallAttempt) -> str | None:
        return attempt.outcome

    def _update_case(self, cursor: Any, case: DispatchCase, expected: int) -> bool:
        cursor.execute(
            """UPDATE dispatch_cases SET state=%s,attempt_count=%s,
               next_attempt_at=%s,final_outcome=%s,closed_at=%s,updated_at=%s,
               version=%s WHERE tenant_id=%s AND dispatch_case_id=%s AND version=%s""",
            (
                case.status.value,
                case.attempt_count,
                case.next_attempt_at,
                case.final_outcome,
                case.closed_at,
                case.updated_at,
                case.version,
                case.tenant_id,
                case.case_id,
                expected,
            ),
        )
        return cursor.rowcount == 1

    def apply_transition(
        self,
        *,
        case: DispatchCase,
        attempt: CallAttempt,
        event: DispatchEvent,
        expected_case_version: int,
        expected_attempt_version: int,
    ) -> bool:
        with self.database.transaction(case.tenant_id) as cursor:
            cursor.execute(
                """SELECT 1 FROM dispatch_events
                   WHERE tenant_id=%s AND dedupe_key_hash=%s""",
                (case.tenant_id, event.deduplication_key),
            )
            if cursor.fetchone() is not None:
                return False
            if not self._update_case(cursor, case, expected_case_version):
                raise DispatchStateConflict("dispatch_repository_conflict")
            cursor.execute(
                """UPDATE dispatch_call_attempts SET state=%s,outcome=%s,
                   initiated_at=COALESCE(initiated_at,%s),
                   answered_at=COALESCE(answered_at,%s),
                   completed_at=COALESCE(completed_at,%s),next_action_at=%s,
                   provider_call_id_hash=COALESCE(provider_call_id_hash,%s),
                   safe_error_code=%s,updated_at=%s,version=%s
                   WHERE tenant_id=%s AND attempt_id=%s AND version=%s""",
                (
                    attempt.status.value,
                    self._outcome(attempt),
                    attempt.initiated_at,
                    attempt.answered_at,
                    attempt.completed_at,
                    attempt.next_action_at,
                    self._provider_hash(attempt.provider_call_reference),
                    attempt.safe_error_code,
                    attempt.updated_at,
                    attempt.version,
                    attempt.tenant_id,
                    attempt.attempt_id,
                    expected_attempt_version,
                ),
            )
            if cursor.rowcount != 1:
                raise DispatchStateConflict("dispatch_repository_conflict")
            if not self._insert_event(cursor, event, attempt=attempt):
                raise DispatchStateConflict("dispatch_repository_conflict")
        return True

    def apply_case_transition(
        self,
        *,
        case: DispatchCase,
        event: DispatchEvent,
        expected_case_version: int,
    ) -> bool:
        with self.database.transaction(case.tenant_id) as cursor:
            cursor.execute(
                """SELECT 1 FROM dispatch_events
                   WHERE tenant_id=%s AND dedupe_key_hash=%s""",
                (case.tenant_id, event.deduplication_key),
            )
            if cursor.fetchone() is not None:
                return False
            if not self._update_case(cursor, case, expected_case_version):
                raise DispatchStateConflict("dispatch_repository_conflict")
            if not self._insert_event(cursor, event, attempt=None):
                raise DispatchStateConflict("dispatch_repository_conflict")
        return True

    def append_event(self, event: DispatchEvent) -> bool:
        with self.database.transaction(event.tenant_id) as cursor:
            attempt = None
            if event.attempt_id is not None:
                cursor.execute(
                    """SELECT * FROM dispatch_call_attempts
                       WHERE tenant_id=%s AND attempt_id=%s""",
                    (event.tenant_id, event.attempt_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise DispatchResourceNotFound()
                attempt = self._attempt(row)
            return self._insert_event(cursor, event, attempt=attempt)

    def resolve_callback(self, callback_token: str) -> tuple[DispatchCase, CallAttempt]:
        with self.database.system_transaction() as cursor:
            cursor.execute(
                """SELECT tenant_id,attempt_id FROM dispatch_callback_routes
                   WHERE callback_token_hash=%s AND expires_at>now()""",
                (_digest(callback_token),),
            )
            route = cursor.fetchone()
        if route is None:
            raise DispatchResourceNotFound()
        tenant_id = str(route["tenant_id"])
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM dispatch_call_attempts
                   WHERE tenant_id=%s AND attempt_id=%s""",
                (tenant_id, str(route["attempt_id"])),
            )
            attempt_row = cursor.fetchone()
            if attempt_row is not None:
                cursor.execute(
                    """SELECT * FROM dispatch_cases
                       WHERE tenant_id=%s AND dispatch_case_id=%s""",
                    (tenant_id, str(attempt_row["dispatch_case_id"])),
                )
                case_row = cursor.fetchone()
            else:
                case_row = None
        if attempt_row is None or case_row is None:
            raise DispatchResourceNotFound()
        return self._case(case_row), self._attempt(attempt_row)

    def list_events(self, tenant_id: str, case_id: str) -> tuple[DispatchEvent, ...]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM dispatch_events
                   WHERE tenant_id=%s AND dispatch_case_id=%s
                   ORDER BY occurred_at,event_id""",
                (tenant_id, case_id),
            )
            rows = cursor.fetchall()
        return tuple(
            DispatchEvent(
                event_id=str(row["event_id"]),
                tenant_id=str(row["tenant_id"]),
                case_id=str(row["dispatch_case_id"]),
                attempt_id=(
                    None if row["attempt_id"] is None else str(row["attempt_id"])
                ),
                kind=DispatchEventKind(row["event_type"]),
                occurred_at=_utc(row["occurred_at"]),
                deduplication_key=row["dedupe_key_hash"],
                detail_code=row["safe_code"],
            )
            for row in rows
        )


__all__ = ["PostgresContactDirectory", "PostgresDispatchRepository"]
