from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "schemas"
FIXTURES = ROOT / "contracts" / "fixtures"
PHASE1_CONTRACTS = (
    "api-error",
    "camera-source",
    "candidate-detection",
    "candidate-review",
    "coverage-snapshot",
    "forecast-feature-row",
    "forecast",
    "tenant-context",
    "video-asset",
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validator(name: str) -> Draft202012Validator:
    schema = load_json(SCHEMAS / f"{name}.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("name", PHASE1_CONTRACTS)
def test_phase1_fixture_matches_schema(name: str) -> None:
    validator(name).validate(load_json(FIXTURES / f"{name}.json"))


def test_future_feature_contract_forbids_observed_label() -> None:
    row = load_json(FIXTURES / "forecast-feature-row.json")
    assert "event_count" not in row
    row["event_count"] = 1

    assert list(validator("forecast-feature-row").iter_errors(row))


@pytest.mark.parametrize(
    ("decision", "removed_field"),
    (("confirmed", "promoted_external_event_id"), ("rejected", "rejection_reason")),
)
def test_review_decision_requires_outcome_specific_fields(
    decision: str, removed_field: str
) -> None:
    review = load_json(FIXTURES / "candidate-review.json")
    review["decision"] = decision
    if decision == "rejected":
        review.pop("confirmed_category")
        review.pop("promoted_external_event_id")
    review.pop(removed_field, None)

    assert list(validator("candidate-review").iter_errors(review))


def test_suppressed_forecast_cannot_publish_zero_as_risk() -> None:
    forecast = load_json(FIXTURES / "forecast.json")
    forecast["suppression"] = {"suppressed": True, "reason": "low_support"}
    forecast["risk_band"] = "suppressed"
    forecast["drivers"] = []
    for field in ("expected_count", "occurrence_probability"):
        forecast[field]["value"] = None
        forecast[field]["lower"] = None
        forecast[field]["upper"] = None
    validator("forecast").validate(forecast)

    invalid = copy.deepcopy(forecast)
    invalid["expected_count"]["value"] = 0
    assert list(validator("forecast").iter_errors(invalid))


def test_live_camera_requires_secret_endpoint_and_credentials() -> None:
    source = load_json(FIXTURES / "camera-source.json")
    source["mode"] = "live_camera"
    source["connection"] = {
        "transport": "rtsp",
        "endpoint_ref": "rtsp://camera.example/live",
        "credential_ref": "secret://tenant/demo-one/cameras/entrance/credential",
    }

    assert list(validator("camera-source").iter_errors(source))


def test_coverage_fixture_obeys_frozen_formula() -> None:
    coverage = load_json(FIXTURES / "coverage-snapshot.json")
    durations = [
        coverage["detector_available_seconds"],
        coverage["processable_seconds"],
        coverage["connected_seconds"],
        coverage["expected_seconds"],
    ]
    assert durations == sorted(durations)
    assert coverage["coverage_ratio"] == pytest.approx(
        coverage["detector_available_seconds"] / coverage["expected_seconds"]
    )


def test_forecast_fixture_is_strictly_future_facing() -> None:
    forecast = load_json(FIXTURES / "forecast.json")
    assert forecast["data_as_of"] < forecast["window_start"]
    for estimate_name in ("expected_count", "occurrence_probability"):
        estimate = forecast[estimate_name]
        assert estimate["lower"] <= estimate["value"] <= estimate["upper"]
