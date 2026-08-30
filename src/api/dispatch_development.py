"""Strictly offline dispatch composition for development and OpenAPI generation.

This module deliberately implements only the browser-facing dispatch contract.  It
never constructs a voice provider, persists a callable destination, or processes a
provider webhook.  Production must inject the durable Postgres/SQS/Twilio
composition instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from pydantic import SecretStr

from .dispatch import (
    DispatchApiDependencies,
    DispatchApiError,
    DispatchCaseView,
    DispatchContactSummary,
    DispatchPreviewView,
    IdempotencyExecutor,
    ResponseContactCreate,
    ResponseContactPage,
    ResponseContactPatch,
    ResponseContactView,
    TestCallView,
    VoicePrompt,
)
from .tenancy import DEMO_TENANT_ONE


@dataclass(frozen=True)
class DevelopmentConfirmedIncident:
    """Minimum restricted fact bundle needed to prepare a safe dispatch preview."""

    incident_id: str
    category: str
    occurred_at: datetime
    zone_id: str = "demo-zone-a"
    zone_label: str = "Demo Zone A"


ConfirmedIncidentResolver = Callable[[str, str], DevelopmentConfirmedIncident | None]


class RejectAllTwilioSignatures:
    """Fail-closed verifier used when no provider callbacks can legitimately exist."""

    def validate(self, uri: str, params: Mapping[str, str], signature: str) -> bool:
        del uri, params, signature
        return False


def _masked(phone: SecretStr | str) -> str:
    value = phone.get_secret_value() if isinstance(phone, SecretStr) else phone
    return f"•••• {value[-4:]}"


def _copy_contact(contact: ResponseContactView) -> ResponseContactView:
    return contact.model_copy(deep=True)


class DevelopmentDispatchService:
    """Process-local, synthetic-only implementation with no external call path."""

    development_only = True
    external_calls_enabled = False

    def __init__(self, resolve_incident: ConfirmedIncidentResolver) -> None:
        self._resolve_incident = resolve_incident
        self._lock = RLock()
        self._contacts: dict[tuple[str, str], ResponseContactView] = {}
        self._cases: dict[tuple[str, str], DispatchCaseView] = {}
        self._case_by_incident: dict[tuple[str, str], str] = {}
        self._case_contact_ids: dict[tuple[str, str], tuple[str, str]] = {}
        self.simulated_test_call_count = 0
        self.external_call_count = 0
        self._seed_synthetic_contacts()

    def _seed_synthetic_contacts(self) -> None:
        created = datetime(2026, 8, 30, 10, 5, tzinfo=UTC)
        for contact_id, display_name, phone_masked, role in (
            (
                "31000000-0000-4000-8000-000000000001",
                "Demo Zone A primary POC",
                "•••• 0182",
                "primary",
            ),
            (
                "31000000-0000-4000-8000-000000000002",
                "Demo Zone A supervisor",
                "•••• 0148",
                "supervisor",
            ),
        ):
            contact = ResponseContactView(
                contact_id=contact_id,
                zone_id="demo-zone-a",
                broad_location_label="Demo Zone A",
                coverage_h3_cells=["8860145b49fffff"],
                display_name=display_name,
                phone_masked=phone_masked,
                role=role,
                enabled=True,
                opted_in_for_demo=True,
                timezone="UTC",
                calling_window_start="00:00",
                calling_window_end="23:59",
                last_verified_at=created,
                created_at=created,
                updated_at=created,
            )
            self._contacts[(DEMO_TENANT_ONE, contact_id)] = contact

    @staticmethod
    def _missing(resource: str) -> DispatchApiError:
        return DispatchApiError(
            404,
            "dispatch_resource_not_found",
            f"The requested {resource} is unavailable",
        )

    def list_response_contacts(
        self,
        *,
        tenant_id: str,
        zone_id: str | None,
        enabled: bool | None,
        limit: int,
        cursor: str | None,
    ) -> ResponseContactPage:
        try:
            offset = 0 if cursor is None else int(cursor)
        except ValueError as error:
            raise DispatchApiError(
                422, "dispatch_cursor_invalid", "The contact cursor is invalid"
            ) from error
        if offset < 0:
            raise DispatchApiError(
                422, "dispatch_cursor_invalid", "The contact cursor is invalid"
            )
        with self._lock:
            items = sorted(
                (
                    value
                    for (scope, _), value in self._contacts.items()
                    if scope == tenant_id
                    and (zone_id is None or value.zone_id == zone_id)
                    and (enabled is None or value.enabled is enabled)
                ),
                key=lambda item: (item.zone_id, item.role, item.contact_id),
            )
            page = items[offset : offset + limit]
            next_offset = offset + len(page)
            return ResponseContactPage(
                items=[_copy_contact(item) for item in page],
                next_cursor=str(next_offset) if next_offset < len(items) else None,
            )

    def get_response_contact(
        self, *, tenant_id: str, contact_id: str
    ) -> ResponseContactView:
        with self._lock:
            contact = self._contacts.get((tenant_id, contact_id))
            if contact is None:
                raise self._missing("response contact")
            return _copy_contact(contact)

    def create_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact: ResponseContactCreate,
    ) -> ResponseContactView:
        del principal_id, request_id
        now = datetime.now(UTC)
        contact_id = str(uuid.uuid4())
        created = ResponseContactView(
            contact_id=contact_id,
            zone_id=contact.zone_id,
            broad_location_label=contact.broad_location_label,
            coverage_h3_cells=list(contact.coverage_h3_cells),
            display_name=contact.display_name,
            # The callable value is intentionally discarded after masking.
            phone_masked=_masked(contact.phone_number),
            role=contact.role,
            enabled=contact.enabled,
            opted_in_for_demo=contact.opted_in_for_demo,
            timezone=contact.timezone,
            calling_window_start=contact.calling_window_start,
            calling_window_end=contact.calling_window_end,
            last_verified_at=contact.last_verified_at,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._contacts[(tenant_id, contact_id)] = created
        return _copy_contact(created)

    def update_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
        changes: ResponseContactPatch,
    ) -> ResponseContactView:
        del principal_id, request_id
        with self._lock:
            current = self._contacts.get((tenant_id, contact_id))
            if current is None:
                raise self._missing("response contact")
            values = changes.model_dump(exclude_unset=True)
            phone_number = values.pop("phone_number", None)
            if phone_number is not None:
                values["phone_masked"] = _masked(phone_number)
            updated = current.model_copy(
                update={**values, "updated_at": datetime.now(UTC)}, deep=True
            )
            # model_copy does not revalidate in Pydantic, so cross the public
            # model boundary once more before persisting the result.
            updated = ResponseContactView.model_validate(
                updated.model_dump(mode="python")
            )
            self._contacts[(tenant_id, contact_id)] = updated
            return _copy_contact(updated)

    def delete_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
    ) -> None:
        del principal_id, request_id
        with self._lock:
            key = (tenant_id, contact_id)
            if key not in self._contacts:
                raise self._missing("response contact")
            if any(
                contact_id in contact_ids
                and self._cases[case_key].state
                not in {
                    "acknowledged",
                    "manual_follow_up",
                    "unacknowledged",
                    "failed",
                    "canceled",
                }
                for case_key, contact_ids in self._case_contact_ids.items()
                if case_key[0] == tenant_id
            ):
                raise DispatchApiError(
                    409,
                    "dispatch_contact_in_use",
                    "An active dispatch case still references this contact",
                )
            del self._contacts[key]

    def create_test_call(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
    ) -> TestCallView:
        del principal_id, request_id
        contact = self.get_response_contact(tenant_id=tenant_id, contact_id=contact_id)
        if not contact.opted_in_for_demo or not contact.enabled:
            raise DispatchApiError(
                409,
                "dispatch_contact_unavailable",
                "The contact is not enabled and opted in for a demo test",
            )
        with self._lock:
            self.simulated_test_call_count += 1
        return TestCallView(
            test_call_id=str(uuid.uuid4()),
            contact_id=contact.contact_id,
            contact_name=contact.display_name,
            phone_masked=contact.phone_masked,
            state="simulated",
            created_at=datetime.now(UTC),
        )

    def _contacts_for(
        self, tenant_id: str, incident: DevelopmentConfirmedIncident
    ) -> tuple[ResponseContactView, ResponseContactView]:
        with self._lock:
            matches = [
                contact
                for (scope, _), contact in self._contacts.items()
                if scope == tenant_id
                and contact.zone_id == incident.zone_id
                and contact.enabled
            ]
        primary = [contact for contact in matches if contact.role == "primary"]
        supervisor = [contact for contact in matches if contact.role == "supervisor"]
        if len(primary) != 1 or len(supervisor) != 1:
            raise DispatchApiError(
                409,
                "dispatch_contact_unavailable",
                "Exactly one enabled primary and supervisor contact is required",
            )
        return primary[0], supervisor[0]

    def _confirmed(
        self, tenant_id: str, incident_id: str
    ) -> DevelopmentConfirmedIncident:
        incident = self._resolve_incident(tenant_id, incident_id)
        if incident is None:
            raise DispatchApiError(
                409,
                "dispatch_incident_unconfirmed",
                "Only a human-confirmed incident may be dispatched",
            )
        return incident

    @staticmethod
    def _summary(contact: ResponseContactView) -> DispatchContactSummary:
        return DispatchContactSummary(
            display_name=contact.display_name,
            phone_masked=contact.phone_masked,
            role=contact.role,
        )

    def preview_dispatch(
        self, *, tenant_id: str, incident_id: str
    ) -> DispatchPreviewView:
        incident = self._confirmed(tenant_id, incident_id)
        primary, supervisor = self._contacts_for(tenant_id, incident)
        return DispatchPreviewView(
            incident_id=incident.incident_id,
            case_reference=self._case_reference(incident.incident_id),
            category=incident.category,
            zone_label=incident.zone_label,
            occurred_at=incident.occurred_at,
            primary_contact=self._summary(primary),
            supervisor_contact=self._summary(supervisor),
            maximum_attempts=3,
            retry_delay_seconds=30,
        )

    @staticmethod
    def _case_reference(incident_id: str) -> str:
        compact = "".join(character for character in incident_id if character.isalnum())
        return f"CH-DEMO-{compact[-8:].upper()}"

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
        del principal_role, request_id, idempotency_key
        incident = self._confirmed(tenant_id, incident_id)
        primary, supervisor = self._contacts_for(tenant_id, incident)
        with self._lock:
            existing_id = self._case_by_incident.get((tenant_id, incident_id))
            if existing_id is not None:
                return self._cases[(tenant_id, existing_id)].model_copy(deep=True)
            now = datetime.now(UTC)
            case_id = str(uuid.uuid4())
            case = DispatchCaseView(
                dispatch_case_id=case_id,
                incident_id=incident.incident_id,
                case_reference=self._case_reference(incident.incident_id),
                category=incident.category,
                zone_label=incident.zone_label,
                occurred_at=incident.occurred_at,
                state="queued",
                message_template_version=message_template_version,
                authorized_by_principal_id=principal_id,
                authorized_at=now,
                primary_contact=self._summary(primary),
                supervisor_contact=self._summary(supervisor),
                attempts=[],
                next_attempt_at=None,
                canceled_at=None,
            )
            self._cases[(tenant_id, case_id)] = case
            self._case_by_incident[(tenant_id, incident_id)] = case_id
            self._case_contact_ids[(tenant_id, case_id)] = (
                primary.contact_id,
                supervisor.contact_id,
            )
            # Intentionally no provider or broker invocation in development.
            return case.model_copy(deep=True)

    def get_dispatch_case(
        self, *, tenant_id: str, dispatch_case_id: str
    ) -> DispatchCaseView:
        with self._lock:
            case = self._cases.get((tenant_id, dispatch_case_id))
            if case is None:
                raise self._missing("dispatch case")
            return case.model_copy(deep=True)

    def cancel_dispatch(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        dispatch_case_id: str,
        reason: str,
    ) -> DispatchCaseView:
        del principal_id, request_id, reason
        with self._lock:
            current = self._cases.get((tenant_id, dispatch_case_id))
            if current is None:
                raise self._missing("dispatch case")
            if current.state in {
                "acknowledged",
                "manual_follow_up",
                "unacknowledged",
                "failed",
                "canceled",
            }:
                if current.state == "canceled":
                    return current.model_copy(deep=True)
                raise DispatchApiError(
                    409,
                    "dispatch_state_conflict",
                    "The dispatch case is already complete",
                )
            updated = current.model_copy(
                update={"state": "canceled", "canceled_at": datetime.now(UTC)},
                deep=True,
            )
            updated = DispatchCaseView.model_validate(updated.model_dump(mode="python"))
            self._cases[(tenant_id, dispatch_case_id)] = updated
            return updated.model_copy(deep=True)

    @staticmethod
    def _webhook_disabled() -> DispatchApiError:
        return DispatchApiError(
            403,
            "mock_webhook_disabled",
            "Voice-provider callbacks are disabled in development mock mode",
        )

    def twilio_voice(
        self, *, opaque_call_token: str, form: Mapping[str, str]
    ) -> VoicePrompt:
        del opaque_call_token, form
        raise self._webhook_disabled()

    def twilio_gather(self, *, opaque_call_token: str, form: Mapping[str, str]) -> None:
        del opaque_call_token, form
        raise self._webhook_disabled()

    def twilio_amd(self, *, opaque_call_token: str, form: Mapping[str, str]) -> None:
        del opaque_call_token, form
        raise self._webhook_disabled()

    def twilio_status(self, *, opaque_call_token: str, form: Mapping[str, str]) -> None:
        del opaque_call_token, form
        raise self._webhook_disabled()


def create_development_dispatch_dependencies(
    *,
    idempotency: IdempotencyExecutor,
    resolve_incident: ConfirmedIncidentResolver,
) -> DispatchApiDependencies:
    """Compose the mock boundary without importing any AWS or Twilio adapter."""

    return DispatchApiDependencies(
        service=DevelopmentDispatchService(resolve_incident),
        idempotency=idempotency,
        signature_verifier=RejectAllTwilioSignatures(),
        public_base_url="https://localhost",
        twilio_mode="mock",
        test_calls_enabled=True,
        external_calls_enabled=False,
    )


__all__ = [
    "DevelopmentConfirmedIncident",
    "DevelopmentDispatchService",
    "RejectAllTwilioSignatures",
    "create_development_dispatch_dependencies",
]
