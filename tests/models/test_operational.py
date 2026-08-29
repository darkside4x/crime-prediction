"""Operational forecast contract, isolation, and suppression tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import numpy as np
import pytest

from src.models.contracts import REPOSITORY_ROOT, validate_contract
from src.models.errors import DataContractError
from src.models.operational import ForecastPolicy, ForecastService

TENANT_ONE = "00000000-0000-4000-8000-000000000001"
TENANT_TWO = "00000000-0000-4000-8000-000000000002"


def future_row() -> dict:
    row = json.loads(
        (REPOSITORY_ROOT / "contracts" / "fixtures" / "forecast-feature-row.json").read_text()
    )
    row["interval_start"] = "2099-08-30T00:00:00Z"
    row["data_as_of"] = "2099-08-29T23:59:59Z"
    row["lag_14"] = 1
    return row


def test_historical_fallback_emits_schema_valid_operational_forecast():
    result = ForecastService().forecast(
        future_row(),
        tenant_id=TENANT_ONE,
        generated_at=datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc),
    )

    validate_contract("forecast", result)
    assert result["model_version"] == "historical-rate-operational-fallback-v1"
    assert result["occurrence_probability"]["calibration_version"] is None
    assert "uncalibrated" in result["occurrence_probability"]["method"]
    assert result["expected_count"]["lower"] <= result["expected_count"]["value"]
    assert result["expected_count"]["value"] <= result["expected_count"]["upper"]


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"coverage_ratio": 0.1}, "low_coverage"),
        ({"lag_1": 0, "lag_2": 0, "lag_7": 0, "lag_14": 0}, "low_support"),
    ],
)
def test_suppression_returns_null_estimates(updates, reason):
    row = future_row()
    row.update(updates)
    result = ForecastService().forecast(
        row,
        tenant_id=TENANT_ONE,
        generated_at=datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc),
    )

    assert result["suppression"] == {"suppressed": True, "reason": reason}
    assert result["risk_band"] == "suppressed"
    assert result["drivers"] == []
    assert set(result["expected_count"].values()) >= {None}
    assert result["expected_count"]["value"] is None
    assert result["occurrence_probability"]["value"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(event_count=1),
        lambda row: row.update(data_as_of=row["interval_start"]),
        lambda row: row.update(tenant_id=TENANT_TWO),
    ],
)
def test_future_inference_rejects_labels_stale_data_and_cross_tenant_rows(mutation):
    row = future_row()
    mutation(row)
    with pytest.raises(DataContractError):
        ForecastService().forecast(
            row,
            tenant_id=TENANT_ONE,
            generated_at=datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc),
        )


def test_generation_after_window_start_is_rejected():
    with pytest.raises(DataContractError, match="after their window starts"):
        ForecastService().forecast(
            future_row(),
            tenant_id=TENANT_ONE,
            generated_at=datetime(2099, 8, 30, 0, 1, tzinfo=timezone.utc),
        )


def test_approved_model_tenant_and_window_must_match():
    class WrongTenantModel:
        tenant_id = TENANT_TWO
        model_version = "approved-v1"
        data_version = "features-v1"
        window_minutes = 360

        def predict(self, rows):
            return np.asarray([0.5])

        def drivers(self, row, limit=5):
            return []

    class Provider:
        def approved_for(self, tenant_id):
            return WrongTenantModel()

    with pytest.raises(DataContractError, match="model tenant"):
        ForecastService(models=Provider()).forecast(
            future_row(),
            tenant_id=TENANT_ONE,
            generated_at=datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc),
        )


def test_policy_rejects_invalid_coverage_threshold():
    with pytest.raises(ValueError):
        ForecastPolicy(minimum_coverage_ratio=1.1)
