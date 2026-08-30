from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from src.api.dispatch import (
    DispatchAttemptView,
    DispatchCaseView,
    DispatchContactSummary,
    ResponseContactView,
)
from src.api.openapi import build_openapi

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "contracts" / "openapi.json"
SCHEMAS = ROOT / "contracts" / "schemas"
FIXTURES = ROOT / "contracts" / "fixtures"
PUBLIC_DISPATCH_COMPONENTS = (
    "ResponseContactView",
    "ResponseContactPage",
    "DispatchContactSummary",
    "DispatchPreviewView",
    "DispatchAttemptView",
    "DispatchCaseView",
)
PUBLIC_DISPATCH_PATHS = {
    "/v1/response-contacts",
    "/v1/response-contacts/{contact_id}",
    "/v1/response-contacts/{contact_id}/test-calls",
    "/v1/incidents/{incident_id}/dispatch-preview",
    "/v1/incidents/{incident_id}/dispatch-authorizations",
    "/v1/dispatch-cases/{dispatch_case_id}",
    "/v1/dispatch-cases/{dispatch_case_id}/cancel",
}


def load_openapi() -> dict[str, Any]:
    return json.loads(OPENAPI.read_text(encoding="utf-8"))


def validate_component(
    document: dict[str, Any], component: str, payload: dict[str, Any]
) -> None:
    root_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/components/schemas/{component}",
        "components": document["components"],
    }
    Draft202012Validator(root_schema, format_checker=FormatChecker()).validate(payload)


def contact_view(phone_masked: str = "****0182") -> ResponseContactView:
    now = datetime(2026, 8, 30, 10, 5, tzinfo=UTC)
    return ResponseContactView(
        contact_id="contact-primary",
        zone_id="demo-zone-a",
        broad_location_label="Demo Zone A",
        coverage_h3_cells=["8860145b49fffff"],
        display_name="Opted-in demo primary",
        phone_masked=phone_masked,
        role="primary",
        enabled=True,
        opted_in_for_demo=True,
        timezone="UTC",
        calling_window_start="00:00",
        calling_window_end="23:59",
        last_verified_at=now,
        created_at=now,
        updated_at=now,
    )


def test_committed_dispatch_openapi_matches_the_runtime_source_of_truth() -> None:
    committed = load_openapi()
    runtime = build_openapi()

    assert PUBLIC_DISPATCH_PATHS <= set(runtime["paths"])
    assert PUBLIC_DISPATCH_PATHS <= set(committed["paths"])
    assert not any(path.startswith("/v1/twilio/") for path in committed["paths"])
    for component in PUBLIC_DISPATCH_COMPONENTS:
        assert (
            committed["components"]["schemas"][component]
            == runtime["components"]["schemas"][component]
        )


def test_dispatch_http_dtos_have_no_parallel_json_schema_or_fixture() -> None:
    for basename in ("response-contact", "dispatch-case", "call-attempt", "call-event"):
        assert not (SCHEMAS / f"{basename}.schema.json").exists()
        assert not (FIXTURES / f"{basename}.json").exists()


def test_openapi_uses_the_actual_browser_field_names() -> None:
    schemas = load_openapi()["components"]["schemas"]
    contact_properties = schemas["ResponseContactView"]["properties"]
    attempt_properties = schemas["DispatchAttemptView"]["properties"]
    case_properties = schemas["DispatchCaseView"]["properties"]

    assert set(contact_properties) == {
        "contact_id",
        "zone_id",
        "broad_location_label",
        "coverage_h3_cells",
        "display_name",
        "phone_masked",
        "role",
        "enabled",
        "opted_in_for_demo",
        "timezone",
        "calling_window_start",
        "calling_window_end",
        "last_verified_at",
        "created_at",
        "updated_at",
    }
    assert set(attempt_properties) == {
        "attempt_id",
        "attempt_number",
        "target_role",
        "contact_name",
        "phone_masked",
        "state",
        "safe_error_code",
        "created_at",
        "updated_at",
    }
    assert set(case_properties) == {
        "dispatch_case_id",
        "incident_id",
        "case_reference",
        "category",
        "zone_label",
        "occurred_at",
        "state",
        "message_template_version",
        "authorized_by_principal_id",
        "authorized_at",
        "primary_contact",
        "supervisor_contact",
        "attempts",
        "next_attempt_at",
        "canceled_at",
    }
    for properties in (contact_properties, attempt_properties, case_properties):
        assert "tenant_id" not in properties
        assert "schema_version" not in properties
        assert "masked_destination" not in properties


def test_public_dispatch_components_contain_no_transport_secrets() -> None:
    schemas = load_openapi()["components"]["schemas"]
    serialized = json.dumps(
        {name: schemas[name] for name in PUBLIC_DISPATCH_COMPONENTS},
        sort_keys=True,
    ).lower()
    for prohibited in (
        "destination_secret_ref",
        "secret_ref",
        "phone_number",
        "provider_call_id",
        "twilio_call_sid",
        "callback_token",
        "call_event",
    ):
        assert prohibited not in serialized


@pytest.mark.parametrize("phone_masked", ["****0182", "••••0182", "•••• 0182"])
def test_response_contact_producer_matches_openapi_and_mask_format(
    phone_masked: str,
) -> None:
    produced = contact_view(phone_masked).model_dump(mode="json")
    validate_component(load_openapi(), "ResponseContactView", produced)
    assert produced["phone_masked"].endswith("0182")


@pytest.mark.parametrize(
    "unsafe_mask", ["+13125550182", "13125550182", "masked", "****82"]
)
def test_response_models_reject_unmasked_or_malformed_destinations(
    unsafe_mask: str,
) -> None:
    with pytest.raises(ValidationError, match="at most four trailing digits"):
        contact_view(unsafe_mask)


def test_dispatch_case_producer_matches_openapi() -> None:
    now = datetime(2026, 8, 30, 12, 30, tzinfo=UTC)
    primary = DispatchContactSummary(
        display_name="Opted-in demo primary",
        phone_masked="****0182",
        role="primary",
    )
    supervisor = DispatchContactSummary(
        display_name="Opted-in demo supervisor",
        phone_masked="****0148",
        role="supervisor",
    )
    attempt = DispatchAttemptView(
        attempt_id="attempt-one",
        attempt_number=1,
        target_role="primary",
        contact_name=primary.display_name,
        phone_masked=primary.phone_masked,
        state="retry_scheduled",
        created_at=now,
        updated_at=now,
    )
    produced = DispatchCaseView(
        dispatch_case_id="dispatch-case-one",
        incident_id="confirmed-incident-one",
        case_reference="CH-DEMO-1042",
        category="traffic_safety",
        zone_label="Demo Zone A",
        occurred_at=now,
        state="retry_scheduled",
        message_template_version="dispatch-alert-v1",
        authorized_by_principal_id="demo-reviewer",
        authorized_at=now,
        primary_contact=primary,
        supervisor_contact=supervisor,
        attempts=[attempt],
        next_attempt_at=now,
    ).model_dump(mode="json")

    validate_component(load_openapi(), "DispatchCaseView", produced)
    assert len(produced["attempts"]) <= 3


@pytest.mark.parametrize(
    ("attempt_number", "target_role"),
    ((1, "supervisor"), (2, "supervisor"), (3, "primary")),
)
def test_attempt_number_cannot_bypass_the_supervisor_escalation_order(
    attempt_number: int, target_role: str
) -> None:
    now = datetime(2026, 8, 30, 12, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="must target"):
        DispatchAttemptView(
            attempt_id="attempt-invalid",
            attempt_number=attempt_number,
            target_role=target_role,
            contact_name="Opted-in demo contact",
            phone_masked="****0182",
            state="queued",
            created_at=now,
            updated_at=now,
        )
