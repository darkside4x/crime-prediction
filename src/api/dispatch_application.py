"""Production application service bridging dispatch HTTP, domain, and AWS adapters."""

from __future__ import annotations

import hashlib
import hmac
import math
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import h3

from src.data.dispatch import (
    CallAttempt,
    CallAttemptStatus,
    ConfirmedIncidentRef,
    DispatchCoordinator,
    DispatchError,
    DispatchStatus,
)
from src.data.dispatch.broker import DispatchBroker
from src.data.dispatch.postgres import (
    PostgresContactDirectory,
    PostgresDispatchRepository,
)
from src.data.dispatch.secrets import PhoneSecretStore, mask_phone
from src.data.postgres import TenantPostgres

from .dispatch import (
    DispatchApiError,
    DispatchAttemptView,
    DispatchCaseView,
    DispatchContactSummary,
    DispatchPreviewView,
    ResponseContactCreate,
    ResponseContactPage,
    ResponseContactPatch,
    ResponseContactView,
    TestCallView,
    VoicePrompt,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _dispatch_error(error: DispatchError) -> DispatchApiError:
    return DispatchApiError(
        error.http_status,
        error.code,
        str(error),
        retryable=error.retryable,
    )


class PostgresDispatchApplicationService:
    """Tenant-isolated response-directory and dispatch use cases."""

    def __init__(
        self,
        *,
        database: TenantPostgres,
        directory: PostgresContactDirectory,
        repository: PostgresDispatchRepository,
        coordinator: DispatchCoordinator,
        broker: DispatchBroker,
        phone_secrets: PhoneSecretStore,
        audit_log: Any,
        max_cases_per_tenant_day: int = 20,
        approved_destination_hashes: frozenset[str] = frozenset(),
        enforce_destination_allowlist: bool = False,
    ) -> None:
        if not 1 <= max_cases_per_tenant_day <= 1000:
            raise ValueError("Dispatch daily quota must be between 1 and 1000")
        self.database = database
        self.directory = directory
        self.repository = repository
        self.coordinator = coordinator
        self.broker = broker
        self.phone_secrets = phone_secrets
        self.audit_log = audit_log
        self.max_cases_per_tenant_day = max_cases_per_tenant_day
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in approved_destination_hashes
        ):
            raise ValueError(
                "Approved destination hashes must be lowercase SHA-256 values"
            )
        if enforce_destination_allowlist and not approved_destination_hashes:
            raise ValueError(
                "An approved destination allowlist is required for external calls"
            )
        self.approved_destination_hashes = approved_destination_hashes
        self.enforce_destination_allowlist = enforce_destination_allowlist

    def _ensure_destination_approved(self, phone_number: str) -> None:
        if not self.enforce_destination_allowlist:
            return
        candidate = hashlib.sha256(phone_number.encode("utf-8")).hexdigest()
        if not any(
            hmac.compare_digest(candidate, approved)
            for approved in self.approved_destination_hashes
        ):
            raise DispatchApiError(
                403,
                "dispatch_destination_not_approved",
                "The destination is not present in the deployment-approved demo allowlist",
            )

    @staticmethod
    def _contact_view(row: Any) -> ResponseContactView:
        return ResponseContactView(
            contact_id=str(row["contact_id"]),
            zone_id=row["zone_id"],
            broad_location_label=row["broad_location_label"],
            coverage_h3_cells=list(row["coverage_h3_cells"]),
            display_name=row["contact_label"],
            phone_masked=row["masked_destination"],
            role=row["role"],
            enabled=bool(row["enabled"]),
            opted_in_for_demo=row["opted_in_at"] is not None,
            timezone=row["timezone"],
            calling_window_start=row["calling_window_start"].strftime("%H:%M"),
            calling_window_end=row["calling_window_end"].strftime("%H:%M"),
            last_verified_at=row["verified_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _contact_row(self, tenant_id: str, contact_id: str) -> Any:
        try:
            uuid.UUID(contact_id)
        except ValueError as error:
            raise DispatchApiError(
                404, "response_contact_not_found", "Response contact was not found"
            ) from error
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                "SELECT * FROM response_contacts WHERE tenant_id=%s AND contact_id=%s",
                (tenant_id, contact_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise DispatchApiError(
                404, "response_contact_not_found", "Response contact was not found"
            )
        return row

    def list_response_contacts(
        self,
        *,
        tenant_id: str,
        zone_id: str | None,
        enabled: bool | None,
        limit: int,
        cursor: str | None,
    ) -> ResponseContactPage:
        arguments: list[Any] = [tenant_id]
        predicates = ["tenant_id=%s"]
        if zone_id is not None:
            predicates.append("zone_id=%s")
            arguments.append(zone_id)
        if enabled is not None:
            predicates.append("enabled=%s")
            arguments.append(enabled)
        if cursor is not None:
            try:
                uuid.UUID(cursor)
            except ValueError as error:
                raise DispatchApiError(
                    422, "cursor_invalid", "Contact cursor is invalid"
                ) from error
            predicates.append("contact_id>%s")
            arguments.append(cursor)
        arguments.append(limit + 1)
        with self.database.transaction(tenant_id) as db_cursor:
            db_cursor.execute(
                f"""SELECT * FROM response_contacts
                     WHERE {" AND ".join(predicates)}
                     ORDER BY contact_id LIMIT %s""",
                tuple(arguments),
            )
            rows = db_cursor.fetchall()
        next_cursor = str(rows[limit - 1]["contact_id"]) if len(rows) > limit else None
        return ResponseContactPage(
            items=[self._contact_view(row) for row in rows[:limit]],
            next_cursor=next_cursor,
        )

    def get_response_contact(
        self, *, tenant_id: str, contact_id: str
    ) -> ResponseContactView:
        return self._contact_view(self._contact_row(tenant_id, contact_id))

    def create_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact: ResponseContactCreate,
    ) -> ResponseContactView:
        contact_id = str(uuid.uuid4())
        phone_number = contact.phone_number.get_secret_value()
        if contact.opted_in_for_demo:
            self._ensure_destination_approved(phone_number)
        secret_ref = self.phone_secrets.create(tenant_id, contact_id, phone_number)
        now = _now()
        opted_in_at = now if contact.opted_in_for_demo else None
        try:
            with self.database.transaction(tenant_id) as cursor:
                cursor.execute(
                    """INSERT INTO response_contacts
                       (tenant_id,contact_id,zone_id,broad_location_label,
                        coverage_h3_cells,role,contact_label,destination_secret_ref,
                        masked_destination,timezone,calling_days,
                        calling_window_start,calling_window_end,enabled,opted_in_at,
                        verified_at,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               ARRAY[0,1,2,3,4,5,6]::smallint[],%s,%s,%s,%s,%s,%s,%s)
                       RETURNING *""",
                    (
                        tenant_id,
                        contact_id,
                        contact.zone_id,
                        contact.broad_location_label,
                        contact.coverage_h3_cells,
                        contact.role,
                        contact.display_name,
                        secret_ref,
                        mask_phone(phone_number),
                        contact.timezone,
                        contact.calling_window_start,
                        contact.calling_window_end,
                        contact.enabled,
                        opted_in_at,
                        contact.last_verified_at,
                        now,
                        now,
                    ),
                )
                row = cursor.fetchone()
        except Exception:
            self.phone_secrets.delete(secret_ref)
            raise
        self._audit(
            tenant_id, principal_id, request_id, "response_contact.create", contact_id
        )
        return self._contact_view(row)

    def update_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
        changes: ResponseContactPatch,
    ) -> ResponseContactView:
        current = self._contact_row(tenant_id, contact_id)
        values = changes.model_dump(exclude_unset=True)
        phone_secret = values.pop("phone_number", None)
        next_opted_in = values.get(
            "opted_in_for_demo", current["opted_in_at"] is not None
        )
        if next_opted_in:
            candidate_phone = (
                phone_secret.get_secret_value()
                if phone_secret is not None
                else self.phone_secrets.resolve(current["destination_secret_ref"])
            )
            self._ensure_destination_approved(candidate_phone)
        old_phone: str | None = None
        if phone_secret is not None:
            old_phone = self.phone_secrets.resolve(current["destination_secret_ref"])
            self.phone_secrets.update(
                current["destination_secret_ref"], phone_secret.get_secret_value()
            )
        mapping = {
            "display_name": "contact_label",
            "last_verified_at": "verified_at",
        }
        if "opted_in_for_demo" in values:
            values["opted_in_at"] = _now() if values.pop("opted_in_for_demo") else None
        if "coverage_h3_cells" in values:
            values["coverage_h3_cells"] = list(values["coverage_h3_cells"])
        values["updated_at"] = _now()
        if phone_secret is not None:
            values["masked_destination"] = mask_phone(phone_secret.get_secret_value())
        assignments: list[str] = []
        parameters: list[Any] = []
        for field, value in values.items():
            column = mapping.get(field, field)
            assignments.append(f"{column}=%s")
            parameters.append(value)
        parameters.extend((tenant_id, contact_id))
        try:
            with self.database.transaction(tenant_id) as cursor:
                cursor.execute(
                    f"""UPDATE response_contacts SET {", ".join(assignments)}
                        WHERE tenant_id=%s AND contact_id=%s RETURNING *""",
                    tuple(parameters),
                )
                row = cursor.fetchone()
        except Exception:
            if old_phone is not None:
                self.phone_secrets.update(current["destination_secret_ref"], old_phone)
            raise
        if row is None:
            raise DispatchApiError(
                404, "response_contact_not_found", "Response contact was not found"
            )
        self._audit(
            tenant_id, principal_id, request_id, "response_contact.update", contact_id
        )
        return self._contact_view(row)

    def delete_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
    ) -> None:
        current = self._contact_row(tenant_id, contact_id)
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT 1 FROM dispatch_cases
                   WHERE tenant_id=%s AND (primary_contact_id=%s OR supervisor_contact_id=%s)
                     AND state NOT IN ('acknowledged','manual_follow_up','unacknowledged','canceled')
                   LIMIT 1""",
                (tenant_id, contact_id, contact_id),
            )
            if cursor.fetchone() is not None:
                raise DispatchApiError(
                    409,
                    "response_contact_in_use",
                    "An active dispatch case still references this contact",
                )
            cursor.execute(
                "DELETE FROM response_contacts WHERE tenant_id=%s AND contact_id=%s",
                (tenant_id, contact_id),
            )
            if cursor.rowcount != 1:
                raise DispatchApiError(
                    404,
                    "response_contact_not_found",
                    "Response contact was not found",
                )
        self.phone_secrets.delete(current["destination_secret_ref"])
        self._audit(
            tenant_id, principal_id, request_id, "response_contact.delete", contact_id
        )

    def create_test_call(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
    ) -> TestCallView:
        contact = self.get_response_contact(tenant_id=tenant_id, contact_id=contact_id)
        if not contact.opted_in_for_demo or not contact.enabled:
            raise DispatchApiError(
                409,
                "contact_not_opted_in",
                "Contact is not enabled and opted in for demonstration calls",
            )
        self._audit(
            tenant_id,
            principal_id,
            request_id,
            "response_contact.test_call",
            contact_id,
        )
        return TestCallView(
            test_call_id=str(uuid.uuid4()),
            contact_id=contact.contact_id,
            contact_name=contact.display_name,
            phone_masked=contact.phone_masked,
            state="simulated",
            created_at=_now(),
        )

    def _incident(self, tenant_id: str, incident_id: str) -> ConfirmedIncidentRef:
        try:
            uuid.UUID(incident_id)
        except ValueError as error:
            raise DispatchApiError(
                404, "incident_not_found", "Incident was not found"
            ) from error
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT review.review_id,review.decision,detection.source_id,
                          incident.external_event_id,incident.category,
                          incident.occurred_at,incident.latitude,incident.longitude
                   FROM candidate_detections_restricted AS detection
                   JOIN candidate_reviews_restricted AS review
                     ON review.tenant_id=detection.tenant_id
                    AND review.detection_id=detection.detection_id
                   JOIN accepted_incident_events_restricted AS incident
                     ON incident.tenant_id=review.tenant_id
                    AND incident.source_id=detection.source_id
                    AND incident.external_event_id=review.decision->>'promoted_external_event_id'
                   WHERE detection.tenant_id=%s AND detection.detection_id=%s
                     AND review.decision->>'decision'='confirmed'""",
                (tenant_id, incident_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise DispatchApiError(
                    409,
                    "dispatch_incident_unconfirmed",
                    "Only a human-confirmed incident may be dispatched",
                )
            cursor.execute(
                """SELECT zone_id,broad_location_label,coverage_h3_cells,role
                   FROM response_contacts
                   WHERE tenant_id=%s AND enabled AND opted_in_at IS NOT NULL""",
                (tenant_id,),
            )
            contact_rows = cursor.fetchall()
        by_zone: dict[tuple[str, str, str], set[str]] = {}
        latitude, longitude = float(row["latitude"]), float(row["longitude"])
        for contact in contact_rows:
            for registered_cell in contact["coverage_h3_cells"]:
                try:
                    observed_cell = h3.latlng_to_cell(
                        latitude, longitude, h3.get_resolution(registered_cell)
                    )
                except (TypeError, ValueError):
                    continue
                if observed_cell == registered_cell:
                    key = (
                        contact["zone_id"],
                        contact["broad_location_label"],
                        registered_cell,
                    )
                    by_zone.setdefault(key, set()).add(contact["role"])
        matches = [
            key for key, roles in by_zone.items() if roles == {"primary", "supervisor"}
        ]
        if len(matches) != 1:
            raise DispatchApiError(
                409,
                "dispatch_contact_unavailable",
                "The response directory cannot resolve one escalation path",
            )
        zone_id, broad_label, cell_id = matches[0]
        return ConfirmedIncidentRef(
            tenant_id=tenant_id,
            incident_id=incident_id,
            confirmed_review_id=str(row["review_id"]),
            incident_source_id=str(row["source_id"]),
            incident_external_event_id=row["external_event_id"],
            cell_id=cell_id,
            zone_id=zone_id,
            case_reference=f"CH-{uuid.UUID(incident_id).hex[:8].upper()}",
            category=row["category"],
            broad_location_label=broad_label,
            occurred_at=row["occurred_at"],
            review_decision="confirmed",
        )

    def _summary(self, tenant_id: str, contact_id: str) -> DispatchContactSummary:
        row = self._contact_row(tenant_id, contact_id)
        return DispatchContactSummary(
            display_name=row["contact_label"],
            phone_masked=row["masked_destination"],
            role=row["role"],
        )

    def preview_dispatch(
        self, *, tenant_id: str, incident_id: str
    ) -> DispatchPreviewView:
        incident = self._incident(tenant_id, incident_id)
        try:
            contacts = self.directory.resolve(tenant_id, incident.coverage, at=_now())
            for contact in (contacts.primary, contacts.supervisor):
                self._ensure_destination_approved(
                    self.phone_secrets.resolve(contact.phone_secret_ref)
                )
        except DispatchError as error:
            raise _dispatch_error(error) from error
        return DispatchPreviewView(
            incident_id=incident.incident_id,
            case_reference=incident.case_reference,
            category=incident.category,
            zone_label=incident.broad_location_label,
            occurred_at=incident.occurred_at,
            primary_contact=self._summary(tenant_id, contacts.primary.contact_id),
            supervisor_contact=self._summary(tenant_id, contacts.supervisor.contact_id),
            maximum_attempts=3,
            retry_delay_seconds=int(
                self.coordinator.policy.retry_delay.total_seconds()
            ),
        )

    def authorize_dispatch(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        principal_role: str,
        request_id: str,
        incident_id: str,
        idempotency_key: str,
        message_template_version: str,
    ) -> DispatchCaseView:
        del principal_role
        incident = self._incident(tenant_id, incident_id)
        try:
            contacts = self.directory.resolve(tenant_id, incident.coverage, at=_now())
            for contact in (contacts.primary, contacts.supervisor):
                self._ensure_destination_approved(
                    self.phone_secrets.resolve(contact.phone_secret_ref)
                )
        except DispatchError as error:
            raise _dispatch_error(error) from error
        if message_template_version != self.coordinator.policy.message_template_version:
            raise DispatchApiError(
                422,
                "dispatch_template_unsupported",
                "Message template version is not allowlisted by this deployment",
            )
        existing = self.repository.find_case_by_idempotency(tenant_id, idempotency_key)
        if existing is None:
            with self.database.transaction(tenant_id) as cursor:
                cursor.execute(
                    """SELECT count(*) AS count FROM dispatch_cases
                       WHERE tenant_id=%s AND authorized_at>=date_trunc('day',now())""",
                    (tenant_id,),
                )
                if int(cursor.fetchone()["count"]) >= self.max_cases_per_tenant_day:
                    raise DispatchApiError(
                        429,
                        "dispatch_daily_quota_exceeded",
                        "Tenant daily dispatch quota has been reached",
                        retryable=True,
                    )
        try:
            case = self.coordinator.authorize(
                incident,
                authorized_by=principal_id,
                idempotency_key=idempotency_key,
                authorize_call=True,
                message_template_version=message_template_version,
            )
        except DispatchError as error:
            raise _dispatch_error(error) from error
        self.broker.enqueue(tenant_id, case.case_id)
        self._audit(
            tenant_id, principal_id, request_id, "dispatch.authorize", case.case_id
        )
        return self._case_view(case)

    @staticmethod
    def _case_state(status: DispatchStatus) -> str:
        return {
            DispatchStatus.QUEUED: "queued",
            DispatchStatus.DIALING: "dialing",
            DispatchStatus.AWAITING_ACKNOWLEDGEMENT: "answered",
            DispatchStatus.PROVIDER_RETRY: "retry_scheduled",
            DispatchStatus.RETRY_SCHEDULED: "retry_scheduled",
            DispatchStatus.SUPERVISOR_SCHEDULED: "escalated",
            DispatchStatus.ACKNOWLEDGED: "acknowledged",
            DispatchStatus.MANUAL_FOLLOW_UP: "manual_follow_up",
            DispatchStatus.UNACKNOWLEDGED: "unacknowledged",
            DispatchStatus.CANCELED: "canceled",
        }[status]

    @staticmethod
    def _attempt_state(status: CallAttemptStatus) -> str:
        return {
            CallAttemptStatus.RESERVED: "queued",
            CallAttemptStatus.PROVIDER_RETRY: "retry_scheduled",
            CallAttemptStatus.INITIATED: "dialing",
            CallAttemptStatus.RINGING: "ringing",
            CallAttemptStatus.ANSWERED: "answered",
            CallAttemptStatus.ACKNOWLEDGED: "acknowledged",
            CallAttemptStatus.MANUAL_FOLLOW_UP: "manual_follow_up",
            CallAttemptStatus.UNACKNOWLEDGED: "unacknowledged",
            CallAttemptStatus.CANCELED: "canceled",
        }[status]

    def _attempt_view(self, attempt: CallAttempt) -> DispatchAttemptView:
        contact = self._contact_row(attempt.tenant_id, attempt.contact_id)
        return DispatchAttemptView(
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.sequence,
            target_role=attempt.target_role.value,
            contact_name=contact["contact_label"],
            phone_masked=contact["masked_destination"],
            state=self._attempt_state(attempt.status),
            safe_error_code=attempt.safe_error_code,
            created_at=attempt.created_at,
            updated_at=attempt.updated_at,
        )

    def _case_view(self, case: Any) -> DispatchCaseView:
        attempts = self.repository.list_attempts(case.tenant_id, case.case_id)
        return DispatchCaseView(
            dispatch_case_id=case.case_id,
            incident_id=case.incident_id,
            case_reference=case.case_reference,
            category=case.category,
            zone_label=case.broad_location_label,
            occurred_at=case.occurred_at,
            state=self._case_state(case.status),
            message_template_version=case.message_template_version,
            authorized_by_principal_id=case.authorized_by,
            authorized_at=case.created_at,
            primary_contact=self._summary(case.tenant_id, case.primary_contact_id),
            supervisor_contact=self._summary(
                case.tenant_id, case.supervisor_contact_id
            ),
            attempts=[self._attempt_view(item) for item in attempts],
            next_attempt_at=case.next_attempt_at,
            canceled_at=(
                case.closed_at if case.status is DispatchStatus.CANCELED else None
            ),
        )

    def get_dispatch_case(
        self, *, tenant_id: str, dispatch_case_id: str
    ) -> DispatchCaseView:
        try:
            return self._case_view(
                self.repository.get_case(tenant_id, dispatch_case_id)
            )
        except DispatchError as error:
            raise _dispatch_error(error) from error

    def cancel_dispatch(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        dispatch_case_id: str,
        reason: str,
    ) -> DispatchCaseView:
        del reason
        try:
            case = self.coordinator.cancel(
                tenant_id,
                dispatch_case_id,
                canceled_by=principal_id,
                event_key=request_id,
            )
        except DispatchError as error:
            raise _dispatch_error(error) from error
        self._audit(
            tenant_id, principal_id, request_id, "dispatch.cancel", dispatch_case_id
        )
        return self._case_view(case)

    def _callback(self, token: str, form: Mapping[str, str]) -> str:
        call_sid = form.get("CallSid", "")
        if not call_sid:
            raise DispatchApiError(
                400, "twilio_call_missing", "Call identifier is required"
            )
        return call_sid

    @staticmethod
    def _event_key(kind: str, form: Mapping[str, str]) -> str:
        encoded = "\x1f".join(
            [
                kind,
                form.get("CallSid", ""),
                form.get("SequenceNumber", ""),
                form.get("CallStatus", ""),
                form.get("AnsweredBy", ""),
                form.get("Digits", ""),
            ]
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def twilio_voice(
        self, *, opaque_call_token: str, form: Mapping[str, str]
    ) -> VoicePrompt:
        call_sid = self._callback(opaque_call_token, form)
        try:
            _, attempt = self.repository.resolve_callback(opaque_call_token)
            expected = attempt.provider_call_reference or ""
            valid = (
                hmac.compare_digest(
                    hashlib.sha256(call_sid.encode()).hexdigest(),
                    expected.removeprefix("sha256:"),
                )
                if expected.startswith("sha256:")
                else hmac.compare_digest(call_sid, expected)
            )
            if not valid:
                raise DispatchApiError(
                    403, "call_mapping_mismatch", "Call mapping did not match"
                )
            script = self.coordinator.voice_script(opaque_call_token)
        except DispatchApiError:
            raise
        except DispatchError as error:
            raise _dispatch_error(error) from error
        return VoicePrompt(
            message=script.message,
            language="en-IN",
            acknowledgement_timeout_seconds=self.coordinator.policy.gather_timeout_seconds,
        )

    def twilio_gather(self, *, opaque_call_token: str, form: Mapping[str, str]) -> None:
        call_sid = self._callback(opaque_call_token, form)
        try:
            case = self.coordinator.handle_gather(
                opaque_call_token,
                provider_call_reference=call_sid,
                digits=form.get("Digits", ""),
                event_key=self._event_key("gather", form),
            )
        except DispatchError as error:
            raise _dispatch_error(error) from error
        self._schedule(case)

    def twilio_amd(self, *, opaque_call_token: str, form: Mapping[str, str]) -> None:
        call_sid = self._callback(opaque_call_token, form)
        try:
            case = self.coordinator.handle_answering_machine(
                opaque_call_token,
                provider_call_reference=call_sid,
                result=form.get("AnsweredBy", "unknown"),
                event_key=self._event_key("amd", form),
            )
        except DispatchError as error:
            raise _dispatch_error(error) from error
        self._schedule(case)

    def twilio_status(self, *, opaque_call_token: str, form: Mapping[str, str]) -> None:
        call_sid = self._callback(opaque_call_token, form)
        try:
            case = self.coordinator.handle_status(
                opaque_call_token,
                provider_call_reference=call_sid,
                status=form.get("CallStatus", ""),
                event_key=self._event_key("status", form),
            )
        except DispatchError as error:
            raise _dispatch_error(error) from error
        self._schedule(case)

    def _schedule(self, case: Any) -> None:
        if case.next_attempt_at is None:
            return
        delay = max(0, math.ceil((case.next_attempt_at - _now()).total_seconds()))
        self.broker.enqueue(case.tenant_id, case.case_id, delay_seconds=min(delay, 900))

    def _audit(
        self,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        action: str,
        resource_id: str,
    ) -> None:
        self.audit_log.record(
            tenant_id=tenant_id,
            principal_id=principal_id,
            request_id=request_id,
            action=action,
            resource_type="dispatch",
            resource_id=resource_id,
        )


__all__ = ["PostgresDispatchApplicationService"]
