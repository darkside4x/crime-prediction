"""Phase 2 authentication, tenancy, forecast, and mutation behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from src.api import reka
from src.api.app import create_app
from src.api.settings import Settings
from src.models.contracts import validate_contract

ONE = {"Authorization": "Bearer demo-token-one"}
TWO = {"Authorization": "Bearer demo-token-two"}
REVIEWER = {"Authorization": "Bearer demo-reviewer-one"}
VIEWER = {"Authorization": "Bearer demo-viewer-one"}


@pytest.fixture()
def app():
    return create_app(provider=reka.FakeRekaProvider(), settings=Settings())


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _future_window() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=6)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")


def test_every_auth_failure_is_a_typed_error(client):
    for headers, code in (
        ({}, "missing_token"),
        ({"Authorization": "Bearer invalid"}, "invalid_token"),
        ({"Authorization": "Bearer expired-demo-token"}, "expired_token"),
    ):
        response = client.get("/v1/metadata", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == code
        validate_contract("api-error", response.json())
        assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_active_tenant_switch_requires_membership_and_is_idempotent(client):
    headers = {**ONE, "Idempotency-Key": "tenant-switch-0001"}
    path = "/v1/me/active-tenant/00000000-0000-4000-8000-000000000002"
    first = client.put(path, headers=headers)
    second = client.put(path, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["role"] == "viewer"

    denied = client.put(
        "/v1/me/active-tenant/00000000-0000-4000-8000-000000000001",
        headers={**TWO, "Idempotency-Key": "tenant-switch-0002"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "tenant_forbidden"


def test_source_mutations_require_idempotency_and_reject_client_tenant(client):
    body = {
        "name": "Entrance recording",
        "timezone": "Asia/Kolkata",
        "registered_location_id": "30000000-0000-4000-8000-000000000001",
        "retention_policy_days": 7,
    }
    missing = client.post("/v1/sources/recorded-video", json=body, headers=ONE)
    assert missing.status_code == 400
    assert missing.json()["code"] == "idempotency_key_required"

    headers = {**ONE, "Idempotency-Key": "recorded-source-0001"}
    first = client.post("/v1/sources/recorded-video", json=body, headers=headers)
    second = client.post("/v1/sources/recorded-video", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert "location_ref" not in first.json()

    smuggled = client.post(
        "/v1/sources/recorded-video",
        json={**body, "tenant_id": "00000000-0000-4000-8000-000000000002"},
        headers={**ONE, "Idempotency-Key": "recorded-source-0002"},
    )
    assert smuggled.status_code == 422
    assert smuggled.json()["code"] == "request_validation_failed"


def test_source_map_location_returns_h3_area_without_raw_coordinates(client):
    response = client.get(
        "/v1/sources/20000000-0000-4000-8000-000000000001/map-location",
        headers=ONE,
    )
    assert response.status_code == 200
    assert response.json()["cell_id"] == "8860145b49fffff"
    assert response.json()["precision"] == "h3_area"
    assert "latitude" not in response.text
    assert "longitude" not in response.text


def test_forecast_endpoint_is_bounded_schema_valid_and_tenant_scoped(client):
    window = _future_window()
    response = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=5", headers=ONE
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 5
    assert body["total"] > 5
    for item in body["items"]:
        validate_contract("forecast", item)
        assert item["tenant_id"].endswith("0001")
        if item["suppression"]["suppressed"]:
            assert item["expected_count"]["value"] is None

    forecast_id = body["items"][0]["forecast_id"]
    assert client.get(f"/v1/forecasts/{forecast_id}", headers=ONE).status_code == 200
    assert client.get(f"/v1/forecasts/{forecast_id}", headers=TWO).status_code == 404

    too_large = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=101", headers=ONE
    )
    assert too_large.status_code == 422
    invalid_bbox = client.get(
        f"/v1/forecasts?window_start={window}&category=property&bbox=20,10,0,30",
        headers=ONE,
    )
    assert invalid_bbox.status_code == 422
    assert invalid_bbox.json()["code"] == "invalid_bbox"


def test_forecast_uses_injected_measured_coverage_not_cell_seed():
    measured_calls: list[tuple[str, str]] = []

    def measured(tenant_id: str, before: str) -> float:
        measured_calls.append((tenant_id, before))
        return 0.82

    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(),
            coverage_provider=measured,
        )
    )
    window = _future_window()
    response = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=5", headers=ONE
    )
    assert response.status_code == 200
    assert {item["coverage_ratio"] for item in response.json()["items"]} == {0.82}
    assert len(measured_calls) == 1


def test_review_role_matrix_and_immutable_idempotent_decision(client):
    assert client.get("/v1/candidate-detections", headers=VIEWER).status_code == 403
    listing = client.get("/v1/candidate-detections", headers=REVIEWER)
    assert listing.status_code == 200
    detection_id = listing.json()["items"][0]["detection_id"]
    assert "evidence_ref" not in listing.text

    body = {"decision": "confirmed", "confirmed_category": "public_order"}
    headers = {**REVIEWER, "Idempotency-Key": "candidate-review-0001"}
    first = client.post(
        f"/v1/candidate-detections/{detection_id}/review", json=body, headers=headers
    )
    replay = client.post(
        f"/v1/candidate-detections/{detection_id}/review", json=body, headers=headers
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()

    overwrite = client.post(
        f"/v1/candidate-detections/{detection_id}/review",
        json={"decision": "rejected", "rejection_reason": "false_positive"},
        headers={**REVIEWER, "Idempotency-Key": "candidate-review-0002"},
    )
    assert overwrite.status_code == 409
    assert overwrite.json()["code"] == "review_final"


def test_secret_configuration_never_appears_in_repr_or_openapi():
    secret = "test-secret-that-must-not-leak"
    settings = Settings(reka_api_key=secret)
    assert secret not in repr(settings)
    app = create_app(provider=reka.FakeRekaProvider(), settings=settings)
    serialized = json.dumps(app.openapi())
    assert "/v1/video-assets/uploads" in app.openapi()["paths"]
    assert "/v1/ingestion/runs/{run_id}" in app.openapi()["paths"]
    assert secret not in serialized
    assert "REKA_API_KEY" not in serialized
    assert "secret_ref" not in serialized


def test_readiness_does_not_treat_an_unverified_reka_key_as_ready():
    settings = Settings(reka_api_key="configured-but-unverified")
    with TestClient(
        create_app(provider=reka.FakeRekaProvider(), settings=settings)
    ) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["reka_chat"] == "configured_unverified"
    assert response.json()["reka_vision"] == "configured_unverified"


def test_synthetic_demo_mode_is_explicitly_labelled_and_unsuppressed():
    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(synthetic_demo_forecasts=True),
        )
    )
    assert client.get("/ready").json()["forecast_data"] == "synthetic_demo"
    assert client.get("/v1/metadata", headers=ONE).json()["forecast_data"] == "synthetic_demo"
    window = _future_window()
    response = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=5",
        headers=ONE,
    )
    assert response.status_code == 200
    assert {item["coverage_ratio"] for item in response.json()["items"]} == {1.0}
    assert any(not item["suppression"]["suppressed"] for item in response.json()["items"])


def test_production_rejects_the_development_authentication_provider():
    with pytest.raises(ValueError, match="production AuthenticationProvider"):
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(app_environment="production"),
        )
