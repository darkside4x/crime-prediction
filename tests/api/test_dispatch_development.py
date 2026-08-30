"""Development dispatch composition stays useful without gaining a call path."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.api import reka
from src.api.app import create_app
from src.api.dispatch_development import DevelopmentDispatchService
from src.api.settings import Settings

ADMIN = {"Authorization": "Bearer demo-token-one"}
REVIEWER = {"Authorization": "Bearer demo-reviewer-one"}


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(runtime_dir=tmp_path),
        )
    )


def test_default_development_openapi_contains_browser_dispatch_contract(tmp_path):
    client = _client(tmp_path)
    document = client.app.openapi()
    paths = document["paths"]

    expected = {
        "/v1/response-contacts",
        "/v1/response-contacts/{contact_id}",
        "/v1/response-contacts/{contact_id}/test-calls",
        "/v1/incidents/{incident_id}/dispatch-preview",
        "/v1/incidents/{incident_id}/dispatch-authorizations",
        "/v1/dispatch-cases/{dispatch_case_id}",
        "/v1/dispatch-cases/{dispatch_case_id}/cancel",
    }
    assert expected <= set(paths)
    assert not any(path.startswith("/v1/twilio/") for path in paths)
    serialized = json.dumps(document)
    assert "TWILIO_ACCOUNT_SID" not in serialized
    assert "TWILIO_AUTH_TOKEN" not in serialized
    assert "secret_ref" not in serialized


def test_development_test_calls_are_simulated_and_discard_callable_number(tmp_path):
    client = _client(tmp_path)
    dependencies = client.app.state.dispatch_dependencies
    service = dependencies.service
    assert isinstance(service, DevelopmentDispatchService)
    assert dependencies.twilio_mode == "mock"
    assert service.external_calls_enabled is False

    created = client.post(
        "/v1/response-contacts",
        headers={**ADMIN, "Idempotency-Key": "dev-contact-create-0001"},
        json={
            "zone_id": "synthetic-zone-b",
            "broad_location_label": "Synthetic Zone B",
            "coverage_h3_cells": ["8860145b49fffff"],
            "display_name": "Synthetic opted-in contact",
            "phone_number": "+15551234567",
            "role": "primary",
            "enabled": True,
            "opted_in_for_demo": True,
            "timezone": "UTC",
            "calling_window_start": "00:00",
            "calling_window_end": "23:59",
            "last_verified_at": "2026-08-30T10:05:00Z",
        },
    )
    assert created.status_code == 201
    assert created.json()["phone_masked"] == "•••• 4567"
    assert "+15551234567" not in repr(service.__dict__)

    simulated = client.post(
        f"/v1/response-contacts/{created.json()['contact_id']}/test-calls",
        headers={**ADMIN, "Idempotency-Key": "dev-test-call-0001"},
        json={"authorize_test_call": True},
    )
    assert simulated.status_code == 202
    assert simulated.json()["state"] == "simulated"
    assert service.simulated_test_call_count == 1
    assert service.external_call_count == 0
    assert client.get("/ready").json()["external_calls_enabled"] is False


def test_confirmed_review_can_queue_mock_case_but_never_calls(tmp_path):
    client = _client(tmp_path)
    service = client.app.state.dispatch_dependencies.service
    listing = client.get("/v1/candidate-detections", headers=REVIEWER)
    detection_id = listing.json()["items"][0]["detection_id"]

    review = client.post(
        f"/v1/candidate-detections/{detection_id}/review",
        headers={**REVIEWER, "Idempotency-Key": "dev-review-confirm-0001"},
        json={"decision": "confirmed", "confirmed_category": "public_order"},
    )
    assert review.status_code == 201
    assert review.json()["promoted_external_event_id"]
    incident_id = detection_id

    preview = client.get(
        f"/v1/incidents/{incident_id}/dispatch-preview", headers=REVIEWER
    )
    assert preview.status_code == 200
    assert preview.json()["maximum_attempts"] == 3

    dispatched = client.post(
        f"/v1/incidents/{incident_id}/dispatch-authorizations",
        headers={**REVIEWER, "Idempotency-Key": "dev-dispatch-authorize-0001"},
        json={
            "authorize_call": True,
            "message_template_version": "dispatch-alert-v1",
        },
    )
    assert dispatched.status_code == 201
    assert dispatched.json()["state"] == "queued"
    assert dispatched.json()["attempts"] == []
    assert service.external_call_count == 0

    arbitrary = client.post(
        "/v1/incidents/22000000-0000-4000-8000-000000000099/dispatch-authorizations",
        headers={**REVIEWER, "Idempotency-Key": "dev-dispatch-reject-0001"},
        json={"authorize_call": True},
    )
    assert arbitrary.status_code == 409
    assert arbitrary.json()["code"] == "dispatch_incident_unconfirmed"
    assert service.external_call_count == 0
