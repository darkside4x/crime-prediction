"""Focused dispatch API authorization, safety, idempotency, and webhook tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from hmac import compare_digest

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dispatch import (
    DispatchApiDependencies,
    DispatchApiError,
    DispatchCaseView,
    DispatchContactSummary,
    DispatchPreviewView,
    ResponseContactCreate,
    ResponseContactPage,
    ResponseContactPatch,
    ResponseContactView,
    VoicePrompt,
    create_dispatch_router,
)
from src.api.dispatch import TestCallView as DispatchTestCallView
from src.api.errors import install_error_handlers
from src.api.state import IdempotencyStore
from src.api.tenancy import (
    DEMO_TENANT_ONE,
    DEMO_TENANT_TWO,
    DevelopmentAuthenticationProvider,
)

ADMIN = {"Authorization": "Bearer demo-token-one"}
REVIEWER = {"Authorization": "Bearer demo-reviewer-one"}
VIEWER = {"Authorization": "Bearer demo-viewer-one"}
NOW = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)
OPAQUE_TOKEN = "opaque_demo_call_token_000001"
CALL_SID = "CA000000000000000000000000000001"
PUBLIC_BASE_URL = "https://api.example.test"


class DeterministicSignatureVerifier:
    """Small offline verifier with the same call shape as Twilio's SDK."""

    def __init__(self, valid_signature: str = "valid-signature") -> None:
        self.valid_signature = valid_signature
        self.calls: list[tuple[str, dict[str, str]]] = []

    def validate(self, uri: str, params: Mapping[str, str], signature: str) -> bool:
        self.calls.append((uri, dict(params)))
        return compare_digest(signature, self.valid_signature)


class FakeDispatchService:
    def __init__(self) -> None:
        self.contacts: dict[tuple[str, str], ResponseContactView] = {}
        self.cases: dict[tuple[str, str], DispatchCaseView] = {}
        self.authorization_calls = 0
        self.test_call_count = 0
        self.webhook_events: list[tuple[str, str, str]] = []
        self.call_mappings = {
            OPAQUE_TOKEN: {"tenant_id": DEMO_TENANT_ONE, "call_sid": CALL_SID}
        }
        self.incident_states = {
            "incident-confirmed": "confirmed",
            "incident-unconfirmed": "unconfirmed",
            "incident-rejected": "rejected",
            "forecast-only-record": "forecast",
        }
        self._seed_contact("contact-primary", "Primary demo POC", "primary")
        self._seed_contact("contact-supervisor", "Demo supervisor", "supervisor")

    def _seed_contact(self, contact_id: str, name: str, role: str) -> None:
        self.contacts[(DEMO_TENANT_ONE, contact_id)] = ResponseContactView(
            contact_id=contact_id,
            zone_id="demo-zone-a",
            broad_location_label="Demo Zone A",
            coverage_h3_cells=["8860145b49fffff"],
            display_name=name,
            phone_masked="••••0101" if role == "primary" else "••••0202",
            role=role,
            enabled=True,
            opted_in_for_demo=True,
            timezone="Asia/Kolkata",
            calling_window_start="00:00",
            calling_window_end="23:59",
            last_verified_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )

    @staticmethod
    def _missing(resource: str) -> DispatchApiError:
        return DispatchApiError(
            404, f"{resource}_not_found", f"{resource} was not found"
        )

    def list_response_contacts(
        self, *, tenant_id, zone_id, enabled, limit, cursor
    ) -> ResponseContactPage:
        del cursor
        values = [
            value
            for (stored_tenant, _), value in self.contacts.items()
            if stored_tenant == tenant_id
            and (zone_id is None or value.zone_id == zone_id)
            and (enabled is None or value.enabled == enabled)
        ]
        return ResponseContactPage(items=values[:limit], next_cursor=None)

    def get_response_contact(self, *, tenant_id, contact_id) -> ResponseContactView:
        try:
            return self.contacts[(tenant_id, contact_id)]
        except KeyError as error:
            raise self._missing("response_contact") from error

    def create_response_contact(
        self, *, tenant_id, principal_id, request_id, contact: ResponseContactCreate
    ) -> ResponseContactView:
        del principal_id, request_id
        contact_id = f"contact-{len(self.contacts) + 1}"
        raw_phone = contact.phone_number.get_secret_value()
        result = ResponseContactView(
            contact_id=contact_id,
            zone_id=contact.zone_id,
            broad_location_label=contact.broad_location_label,
            coverage_h3_cells=contact.coverage_h3_cells,
            display_name=contact.display_name,
            phone_masked=f"••••{raw_phone[-4:]}",
            role=contact.role,
            enabled=contact.enabled,
            opted_in_for_demo=contact.opted_in_for_demo,
            timezone=contact.timezone,
            calling_window_start=contact.calling_window_start,
            calling_window_end=contact.calling_window_end,
            last_verified_at=contact.last_verified_at,
            created_at=NOW,
            updated_at=NOW,
        )
        self.contacts[(tenant_id, contact_id)] = result
        return result

    def update_response_contact(
        self,
        *,
        tenant_id,
        principal_id,
        request_id,
        contact_id,
        changes: ResponseContactPatch,
    ) -> ResponseContactView:
        del principal_id, request_id
        current = self.get_response_contact(tenant_id=tenant_id, contact_id=contact_id)
        update = changes.model_dump(exclude_unset=True)
        phone = update.pop("phone_number", None)
        if phone is not None:
            update["phone_masked"] = f"••••{phone.get_secret_value()[-4:]}"
        result = current.model_copy(update={**update, "updated_at": NOW})
        self.contacts[(tenant_id, contact_id)] = result
        return result

    def delete_response_contact(
        self, *, tenant_id, principal_id, request_id, contact_id
    ) -> None:
        del principal_id, request_id
        if self.contacts.pop((tenant_id, contact_id), None) is None:
            raise self._missing("response_contact")

    def create_test_call(
        self, *, tenant_id, principal_id, request_id, contact_id
    ) -> DispatchTestCallView:
        del principal_id, request_id
        contact = self.get_response_contact(tenant_id=tenant_id, contact_id=contact_id)
        if not contact.opted_in_for_demo:
            raise DispatchApiError(
                409,
                "contact_not_opted_in",
                "Contact has not opted in to demonstration calls",
            )
        self.test_call_count += 1
        return DispatchTestCallView(
            test_call_id=f"test-call-{self.test_call_count}",
            contact_id=contact.contact_id,
            contact_name=contact.display_name,
            phone_masked=contact.phone_masked,
            state="simulated",
            created_at=NOW,
        )

    def authorize_dispatch(
        self,
        *,
        tenant_id,
        principal_id,
        principal_role,
        request_id,
        incident_id,
        idempotency_key,
        message_template_version,
    ) -> DispatchCaseView:
        del principal_role, request_id, idempotency_key
        state = self.incident_states.get(incident_id)
        if state is None:
            raise self._missing("incident")
        if state == "unconfirmed":
            raise DispatchApiError(
                409,
                "dispatch_incident_unconfirmed",
                "Only a human-confirmed incident may be dispatched",
            )
        if state == "rejected":
            raise DispatchApiError(
                409,
                "dispatch_incident_rejected",
                "A rejected detection cannot be dispatched",
            )
        if state == "forecast":
            raise DispatchApiError(
                422,
                "dispatch_source_invalid",
                "Forecasts and candidates cannot authorize dispatch",
            )
        self.authorization_calls += 1
        case_id = "dispatch-case-0001"
        existing = self.cases.get((tenant_id, case_id))
        if existing is not None:
            return existing
        result = DispatchCaseView(
            dispatch_case_id=case_id,
            incident_id=incident_id,
            case_reference="CH-1042",
            category="traffic_safety",
            zone_label="Demo Zone A",
            occurred_at=NOW,
            state="queued",
            message_template_version=message_template_version,
            authorized_by_principal_id=principal_id,
            authorized_at=NOW,
            primary_contact=DispatchContactSummary(
                display_name="Primary demo POC",
                phone_masked="••••0101",
                role="primary",
            ),
            supervisor_contact=DispatchContactSummary(
                display_name="Demo supervisor",
                phone_masked="••••0202",
                role="supervisor",
            ),
            attempts=[],
        )
        self.cases[(tenant_id, case_id)] = result
        return result

    def preview_dispatch(self, *, tenant_id, incident_id) -> DispatchPreviewView:
        if (
            tenant_id != DEMO_TENANT_ONE
            or self.incident_states.get(incident_id) != "confirmed"
        ):
            raise self._missing("incident")
        return DispatchPreviewView(
            incident_id=incident_id,
            case_reference="CH-1042",
            category="traffic_safety",
            zone_label="Demo Zone A",
            occurred_at=NOW,
            primary_contact=DispatchContactSummary(
                display_name="Primary demo POC",
                phone_masked="••••0101",
                role="primary",
            ),
            supervisor_contact=DispatchContactSummary(
                display_name="Demo supervisor",
                phone_masked="••••0202",
                role="supervisor",
            ),
            maximum_attempts=3,
            retry_delay_seconds=30,
        )

    def get_dispatch_case(self, *, tenant_id, dispatch_case_id) -> DispatchCaseView:
        try:
            return self.cases[(tenant_id, dispatch_case_id)]
        except KeyError as error:
            raise self._missing("dispatch_case") from error

    def cancel_dispatch(
        self,
        *,
        tenant_id,
        principal_id,
        request_id,
        dispatch_case_id,
        reason,
    ) -> DispatchCaseView:
        del principal_id, request_id, reason
        current = self.get_dispatch_case(
            tenant_id=tenant_id, dispatch_case_id=dispatch_case_id
        )
        result = current.model_copy(update={"state": "canceled", "canceled_at": NOW})
        self.cases[(tenant_id, dispatch_case_id)] = result
        return result

    def _mapping(self, token: str, form: Mapping[str, str]) -> dict[str, str]:
        mapping = self.call_mappings.get(token)
        if mapping is None:
            raise DispatchApiError(
                404, "call_mapping_not_found", "Call mapping was not found"
            )
        supplied_sid = form.get("CallSid")
        if supplied_sid is not None and supplied_sid != mapping["call_sid"]:
            raise DispatchApiError(
                403, "call_mapping_mismatch", "Call mapping did not match"
            )
        return mapping

    def twilio_voice(self, *, opaque_call_token, form) -> VoicePrompt:
        mapping = self._mapping(opaque_call_token, form)
        self.webhook_events.append((mapping["tenant_id"], "voice", opaque_call_token))
        return VoicePrompt(
            message=(
                "CivicHalo demo alert. Human-confirmed traffic-safety incident, "
                "case CH-1042, near Demo Zone A at 18:00 UTC. Press 1 to "
                "acknowledge or 2 to request a callback."
            )
        )

    def twilio_gather(self, *, opaque_call_token, form) -> None:
        mapping = self._mapping(opaque_call_token, form)
        self.webhook_events.append(
            (
                mapping["tenant_id"],
                f"gather:{form.get('Digits', '')}",
                opaque_call_token,
            )
        )

    def twilio_amd(self, *, opaque_call_token, form) -> None:
        mapping = self._mapping(opaque_call_token, form)
        self.webhook_events.append(
            (
                mapping["tenant_id"],
                f"amd:{form.get('AnsweredBy', '')}",
                opaque_call_token,
            )
        )

    def twilio_status(self, *, opaque_call_token, form) -> None:
        mapping = self._mapping(opaque_call_token, form)
        # Deliberately ignore callback-supplied tenant data and use the mapping.
        self.webhook_events.append(
            (
                mapping["tenant_id"],
                f"status:{form.get('CallStatus', '')}",
                opaque_call_token,
            )
        )


def _build_client(
    *,
    service: FakeDispatchService | None = None,
    test_calls_enabled: bool = False,
    twilio_mode: str = "mock",
    external_calls_enabled: bool = False,
) -> tuple[TestClient, FakeDispatchService, DeterministicSignatureVerifier]:
    service = service or FakeDispatchService()
    verifier = DeterministicSignatureVerifier()
    app = FastAPI()
    app.state.auth_provider = DevelopmentAuthenticationProvider()
    install_error_handlers(app)
    app.include_router(
        create_dispatch_router(
            DispatchApiDependencies(
                service=service,
                idempotency=IdempotencyStore(),
                signature_verifier=verifier,
                public_base_url=PUBLIC_BASE_URL,
                twilio_mode=twilio_mode,
                test_calls_enabled=test_calls_enabled,
                external_calls_enabled=external_calls_enabled,
            )
        )
    )
    return TestClient(app), service, verifier


@pytest.fixture()
def dispatch_client():
    client, service, verifier = _build_client()
    with client:
        yield client, service, verifier


def _contact_payload(phone: str = "+15555550101") -> dict:
    return {
        "zone_id": "demo-zone-b",
        "broad_location_label": "Demo Zone B",
        "coverage_h3_cells": ["8860145b49fffff"],
        "display_name": "Opted-in teammate",
        "phone_number": phone,
        "role": "primary",
        "enabled": True,
        "opted_in_for_demo": True,
        "timezone": "Asia/Kolkata",
        "calling_window_start": "00:00",
        "calling_window_end": "23:59",
        "last_verified_at": "2026-08-30T18:00:00Z",
    }


def test_contact_crud_is_admin_only_and_masks_phone(dispatch_client):
    client, _, _ = dispatch_client
    raw_phone = "+15555550101"
    denied = client.get("/v1/response-contacts", headers=REVIEWER)
    assert denied.status_code == 403
    assert denied.json()["code"] == "role_forbidden"

    created = client.post(
        "/v1/response-contacts",
        json=_contact_payload(raw_phone),
        headers={**ADMIN, "Idempotency-Key": "contact-create-0001"},
    )
    assert created.status_code == 201, created.text
    contact_id = created.json()["contact_id"]
    assert created.json()["phone_masked"] == "••••0101"
    assert raw_phone not in created.text
    assert "phone_number" not in created.text
    assert "secret_ref" not in created.text

    listing = client.get("/v1/response-contacts?zone_id=demo-zone-b", headers=ADMIN)
    assert listing.status_code == 200
    assert [item["contact_id"] for item in listing.json()["items"]] == [contact_id]
    assert raw_phone not in listing.text

    updated = client.patch(
        f"/v1/response-contacts/{contact_id}",
        json={"phone_number": "+15555550999", "enabled": False},
        headers={**ADMIN, "Idempotency-Key": "contact-update-0001"},
    )
    assert updated.status_code == 200
    assert updated.json()["phone_masked"] == "••••0999"
    assert "+15555550999" not in updated.text

    deleted = client.delete(
        f"/v1/response-contacts/{contact_id}",
        headers={**ADMIN, "Idempotency-Key": "contact-delete-0001"},
    )
    assert deleted.status_code == 204
    assert (
        client.get(f"/v1/response-contacts/{contact_id}", headers=ADMIN).status_code
        == 404
    )


def test_test_call_requires_explicit_safe_deployment_gate(dispatch_client):
    client, service, _ = dispatch_client
    path = "/v1/response-contacts/contact-primary/test-calls"
    disabled = client.post(
        path,
        json={"authorize_test_call": True},
        headers={**ADMIN, "Idempotency-Key": "test-call-0001"},
    )
    assert disabled.status_code == 403
    assert disabled.json()["code"] == "test_call_disabled"
    assert service.test_call_count == 0

    enabled_client, enabled_service, _ = _build_client(test_calls_enabled=True)
    with enabled_client:
        implicit = enabled_client.post(
            path,
            json={"authorize_test_call": False},
            headers={**ADMIN, "Idempotency-Key": "test-call-0002"},
        )
        assert implicit.status_code == 422
        allowed = enabled_client.post(
            path,
            json={"authorize_test_call": True},
            headers={**ADMIN, "Idempotency-Key": "test-call-0003"},
        )
        assert allowed.status_code == 202
        assert allowed.json()["state"] == "simulated"
        assert CALL_SID not in allowed.text
        assert enabled_service.test_call_count == 1

    live_client, live_service, _ = _build_client(
        test_calls_enabled=True, twilio_mode="live"
    )
    with live_client:
        live = live_client.post(
            path,
            json={"authorize_test_call": True},
            headers={**ADMIN, "Idempotency-Key": "test-call-0004"},
        )
        assert live.status_code == 403
        assert live_service.test_call_count == 0


def test_dispatch_requires_reviewer_explicit_authorization_and_confirmed_incident(
    dispatch_client,
):
    client, service, _ = dispatch_client
    path = "/v1/incidents/incident-confirmed/dispatch-authorizations"
    viewer = client.post(
        path,
        json={"authorize_call": True},
        headers={**VIEWER, "Idempotency-Key": "dispatch-role-0001"},
    )
    assert viewer.status_code == 403
    assert service.authorization_calls == 0
    implicit = client.post(
        path,
        json={"authorize_call": False},
        headers={**REVIEWER, "Idempotency-Key": "dispatch-explicit-0001"},
    )
    assert implicit.status_code == 422
    assert service.authorization_calls == 0

    for incident_id, expected_status, expected_code in (
        ("incident-unconfirmed", 409, "dispatch_incident_unconfirmed"),
        ("incident-rejected", 409, "dispatch_incident_rejected"),
        ("forecast-only-record", 422, "dispatch_source_invalid"),
    ):
        response = client.post(
            f"/v1/incidents/{incident_id}/dispatch-authorizations",
            json={"authorize_call": True},
            headers={**REVIEWER, "Idempotency-Key": f"dispatch-{incident_id}"},
        )
        assert response.status_code == expected_status
        assert response.json()["code"] == expected_code

    confirmed = client.post(
        path,
        json={"authorize_call": True},
        headers={**REVIEWER, "Idempotency-Key": "dispatch-confirmed-0001"},
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["state"] == "queued"
    assert confirmed.json()["primary_contact"]["phone_masked"] == "••••0101"
    assert CALL_SID not in confirmed.text
    assert "secret_ref" not in confirmed.text


def test_live_dispatch_requires_deployment_kill_switch() -> None:
    client, service, _ = _build_client(twilio_mode="live")
    with client:
        response = client.post(
            "/v1/incidents/incident-confirmed/dispatch-authorizations",
            json={"authorize_call": True},
            headers={**REVIEWER, "Idempotency-Key": "dispatch-live-gate-0001"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "external_calls_disabled"
    assert service.authorization_calls == 0


def test_dispatch_authorization_is_idempotent_and_payload_bound(dispatch_client):
    client, service, _ = dispatch_client
    path = "/v1/incidents/incident-confirmed/dispatch-authorizations"
    headers = {**REVIEWER, "Idempotency-Key": "dispatch-duplicate-0001"}
    first = client.post(path, json={"authorize_call": True}, headers=headers)
    replay = client.post(path, json={"authorize_call": True}, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert service.authorization_calls == 1

    conflict = client.post(
        path,
        json={
            "authorize_call": True,
            "message_template_version": "dispatch-alert-v2",
        },
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    assert service.authorization_calls == 1


def test_dispatch_case_cancel_is_reviewer_only_and_idempotent(dispatch_client):
    client, _, _ = dispatch_client
    created = client.post(
        "/v1/incidents/incident-confirmed/dispatch-authorizations",
        json={"authorize_call": True},
        headers={**REVIEWER, "Idempotency-Key": "dispatch-cancel-create"},
    )
    case_id = created.json()["dispatch_case_id"]
    assert (
        client.get(f"/v1/dispatch-cases/{case_id}", headers=VIEWER).status_code == 403
    )
    headers = {**REVIEWER, "Idempotency-Key": "dispatch-cancel-0001"}
    first = client.post(
        f"/v1/dispatch-cases/{case_id}/cancel",
        json={"cancel_pending_calls": True, "reason": "Demo rehearsal ended"},
        headers=headers,
    )
    replay = client.post(
        f"/v1/dispatch-cases/{case_id}/cancel",
        json={"cancel_pending_calls": True, "reason": "Demo rehearsal ended"},
        headers=headers,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["state"] == "canceled"


def test_invalid_twilio_signature_cannot_change_state(dispatch_client):
    client, service, verifier = dispatch_client
    path = f"/v1/twilio/status/{OPAQUE_TOKEN}"
    response = client.post(
        path,
        data={"CallSid": CALL_SID, "CallStatus": "completed"},
        headers={"X-Twilio-Signature": "invalid-signature"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "invalid_webhook_signature"
    assert service.webhook_events == []
    assert verifier.calls[0][0] == f"{PUBLIC_BASE_URL}{path}"


def test_signed_twilio_webhooks_need_no_jwt_and_resolve_opaque_mapping(dispatch_client):
    client, service, _ = dispatch_client
    headers = {"X-Twilio-Signature": "valid-signature"}

    voice = client.post(
        f"/v1/twilio/voice/{OPAQUE_TOKEN}",
        data={"CallSid": CALL_SID},
        headers=headers,
    )
    assert voice.status_code == 200
    assert voice.headers["content-type"].startswith("application/xml")
    assert "Human-confirmed traffic-safety incident" in voice.text
    assert f"{PUBLIC_BASE_URL}/v1/twilio/gather/{OPAQUE_TOKEN}" in voice.text
    assert CALL_SID not in voice.text

    callbacks = (
        ("gather", {"CallSid": CALL_SID, "Digits": "1"}),
        ("amd", {"CallSid": CALL_SID, "AnsweredBy": "human"}),
        (
            "status",
            {
                "CallSid": CALL_SID,
                "CallStatus": "completed",
                "tenant_id": DEMO_TENANT_TWO,
            },
        ),
    )
    for endpoint, body in callbacks:
        response = client.post(
            f"/v1/twilio/{endpoint}/{OPAQUE_TOKEN}", data=body, headers=headers
        )
        assert response.status_code == 200
        assert CALL_SID not in response.text

    assert len(service.webhook_events) == 4
    assert {event[0] for event in service.webhook_events} == {DEMO_TENANT_ONE}


def test_signed_webhook_rejects_unknown_token_and_call_sid_mismatch(dispatch_client):
    client, service, _ = dispatch_client
    headers = {"X-Twilio-Signature": "valid-signature"}
    unknown = client.post(
        "/v1/twilio/status/opaque_unknown_call_token_00001",
        data={"CallSid": CALL_SID, "CallStatus": "ringing"},
        headers=headers,
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "call_mapping_not_found"

    mismatch = client.post(
        f"/v1/twilio/status/{OPAQUE_TOKEN}",
        data={"CallSid": "CA-wrong", "CallStatus": "completed"},
        headers=headers,
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "call_mapping_mismatch"
    assert service.webhook_events == []


def test_public_response_models_reject_unmasked_phone_numbers():
    with pytest.raises(ValueError, match="at most four"):
        DispatchContactSummary(
            display_name="Unsafe",
            phone_masked="+15555550101",
            role="primary",
        )


def test_openapi_exposes_browser_contract_without_webhook_or_provider_secrets():
    client, _, _ = _build_client()
    serialized = str(client.app.openapi())
    assert "/v1/incidents/{incident_id}/dispatch-authorizations" in serialized
    assert "/v1/response-contacts" in serialized
    assert "/v1/twilio" not in serialized
    assert "CallSid" not in serialized
    assert "secret_ref" not in serialized


def test_public_base_url_must_be_https():
    service = FakeDispatchService()
    with pytest.raises(ValueError, match="HTTPS"):
        DispatchApiDependencies(
            service=service,
            idempotency=IdempotencyStore(),
            signature_verifier=DeterministicSignatureVerifier(),
            public_base_url="http://api.example.test",
        )
