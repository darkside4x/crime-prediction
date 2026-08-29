"""Operational inference for unlabelled future feature snapshots.

Evaluation predictions and operational forecasts intentionally use separate
contracts.  This module accepts only ``forecast-feature-row`` records and emits
only ``forecast`` records; it never accepts a future label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Protocol
import uuid

import numpy as np

from .contracts import validate_contract
from .data import parse_utc
from .errors import DataContractError


class OperationalEstimator(Protocol):
    """Minimum interface exposed by an approved tenant model."""

    tenant_id: str
    model_version: str
    data_version: str
    window_minutes: int

    def predict(self, rows: list[dict[str, Any]]) -> np.ndarray: ...

    def drivers(self, row: dict[str, Any], limit: int = 5) -> list[dict[str, str]]: ...


class ModelProvider(Protocol):
    def approved_for(self, tenant_id: str) -> OperationalEstimator | None: ...


class NoApprovedModelProvider:
    """Default provider used until a reviewed tenant bundle is promoted."""

    def approved_for(self, tenant_id: str) -> None:
        return None


@dataclass(frozen=True)
class ForecastPolicy:
    window_minutes: int = 360
    minimum_recent_support: float = 3.0
    minimum_coverage_ratio: float = 0.5
    interval_level: float = 0.9
    risk_band_thresholds: tuple[float, float, float] = (0.2, 0.5, 0.75)

    def __post_init__(self) -> None:
        if self.window_minutes < 1:
            raise ValueError("window_minutes must be positive")
        if self.minimum_recent_support < 0:
            raise ValueError("minimum_recent_support cannot be negative")
        if not 0 <= self.minimum_coverage_ratio <= 1:
            raise ValueError("minimum_coverage_ratio must be between zero and one")
        if not 0 < self.interval_level < 1:
            raise ValueError("interval_level must be between zero and one")
        if tuple(sorted(self.risk_band_thresholds)) != self.risk_band_thresholds:
            raise ValueError("risk_band_thresholds must be ordered")


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _empirical_interval(row: dict[str, Any], expected: float) -> tuple[float, float]:
    """Conservative interval from comparable, strictly historical windows."""
    historical = np.asarray(
        [
            row["lag_1"],
            row["lag_2"],
            row["lag_7"],
            row["lag_14"],
            row["rolling_7_mean"],
            row["rolling_14_mean"],
        ],
        dtype=float,
    )
    lower, upper = np.quantile(historical, [0.05, 0.95])
    return max(0.0, min(float(lower), expected)), max(float(upper), expected)


def _fallback_expected_count(row: dict[str, Any]) -> float:
    """Comparable-window historical rate with recent/seasonal balance."""
    recent = (float(row["lag_1"]) + float(row["lag_2"]) + float(row["rolling_7_mean"])) / 3
    seasonal = (float(row["lag_7"]) + float(row["lag_14"]) + float(row["rolling_14_mean"])) / 3
    return max(0.0, 0.5 * recent + 0.5 * seasonal)


def _risk_band(probability: float, thresholds: tuple[float, float, float]) -> str:
    if probability < thresholds[0]:
        return "low"
    if probability < thresholds[1]:
        return "typical"
    if probability < thresholds[2]:
        return "elevated"
    return "high"


class ForecastService:
    """Generate tenant-scoped forecasts with safe suppression and fallback."""

    def __init__(
        self,
        models: ModelProvider | None = None,
        policy: ForecastPolicy | None = None,
    ) -> None:
        self._models = models or NoApprovedModelProvider()
        self.policy = policy or ForecastPolicy()

    def forecast(
        self,
        raw_row: dict[str, Any],
        *,
        tenant_id: str,
        generated_at: datetime | None = None,
    ) -> dict[str, Any]:
        validate_contract("forecast-feature-row", raw_row)
        if raw_row["tenant_id"] != tenant_id:
            raise DataContractError("Future feature snapshot tenant does not match authenticated tenant")

        interval_start = parse_utc(raw_row["interval_start"])
        data_as_of = parse_utc(raw_row["data_as_of"])
        generated = generated_at or datetime.now(timezone.utc)
        if generated.tzinfo is None or generated.utcoffset() != timedelta(0):
            raise DataContractError("generated_at must be timezone-aware UTC")
        generated = generated.astimezone(timezone.utc)
        if data_as_of >= interval_start:
            raise DataContractError("data_as_of must be strictly before the forecast interval")
        if generated > interval_start:
            raise DataContractError("Operational forecasts cannot be generated after their window starts")

        row = dict(raw_row)
        row["interval_start"] = interval_start
        row["data_as_of"] = data_as_of
        model = self._models.approved_for(tenant_id)
        if model is not None:
            if model.tenant_id != tenant_id:
                raise DataContractError("Approved model tenant does not match authenticated tenant")
            if model.window_minutes != self.policy.window_minutes:
                raise DataContractError("Approved model interval does not match forecast policy")
            expected = float(model.predict([row])[0])
            if not math.isfinite(expected) or expected < 0:
                raise DataContractError("Approved model produced an invalid aggregate count")
            model_version = model.model_version
            data_version = model.data_version
            drivers = model.drivers(row, limit=5)
            calibrate = getattr(model, "calibrate_probability", None)
            interval = getattr(model, "count_interval", None)
            calibration_version = getattr(model, "calibration_version", None)
        else:
            expected = _fallback_expected_count(row)
            model_version = "historical-rate-operational-fallback-v1"
            data_version = raw_row["feature_snapshot_version"]
            drivers = [
                {"feature": "historical comparable-window rate", "direction": "higher"}
            ]
            probability_method = "poisson_link_fallback_uncalibrated"
            calibration_version = None
            calibrate = None
            interval = None

        recent_support = sum(float(raw_row[name]) for name in ("lag_1", "lag_2", "lag_7", "lag_14"))
        reason: str | None = None
        if float(raw_row["coverage_ratio"]) < self.policy.minimum_coverage_ratio:
            reason = "low_coverage"
        elif recent_support < self.policy.minimum_recent_support:
            reason = "low_support"

        if callable(interval):
            count_lower, count_upper, count_interval_method = interval(row, expected)
        else:
            count_lower, count_upper = _empirical_interval(row, expected)
            count_interval_method = "comparable_window_empirical_interval_v1"
        raw_probability = 1.0 - math.exp(-expected)
        raw_probability_lower = 1.0 - math.exp(-count_lower)
        raw_probability_upper = 1.0 - math.exp(-count_upper)
        if callable(calibrate):
            probability = float(calibrate(raw_probability))
            probability_lower = min(probability, float(calibrate(raw_probability_lower)))
            probability_upper = max(probability, float(calibrate(raw_probability_upper)))
            probability_method = "validation_isotonic_pav_v1"
        else:
            probability = raw_probability
            probability_lower = raw_probability_lower
            probability_upper = raw_probability_upper
        suppressed = reason is not None
        forecast_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join(
                    (
                        tenant_id,
                        raw_row["cell_id"],
                        raw_row["interval_start"],
                        raw_row["category"],
                        raw_row["feature_snapshot_version"],
                        model_version,
                    )
                ),
            )
        )
        null_or = lambda value: None if suppressed else float(value)
        result = {
            "schema_version": "1.0.0",
            "tenant_id": tenant_id,
            "forecast_id": forecast_id,
            "cell_id": raw_row["cell_id"],
            "window_start": _format_utc(interval_start),
            "window_end": _format_utc(interval_start + timedelta(minutes=self.policy.window_minutes)),
            "category": raw_row["category"],
            "generated_at": _format_utc(generated),
            "data_as_of": _format_utc(data_as_of),
            "expected_count": {
                "value": null_or(expected),
                "lower": null_or(count_lower),
                "upper": null_or(count_upper),
                "interval_level": self.policy.interval_level,
                "method": count_interval_method,
            },
            "occurrence_probability": {
                "value": null_or(probability),
                "lower": null_or(probability_lower),
                "upper": null_or(probability_upper),
                "interval_level": self.policy.interval_level,
                "method": probability_method,
                "calibration_version": calibration_version,
            },
            "risk_band": "suppressed" if suppressed else _risk_band(probability, self.policy.risk_band_thresholds),
            "coverage_ratio": float(raw_row["coverage_ratio"]),
            "drivers": [] if suppressed else drivers,
            "model_version": model_version,
            "data_version": data_version,
            "feature_snapshot_version": raw_row["feature_snapshot_version"],
            "suppression": {"suppressed": suppressed, "reason": reason},
        }
        validate_contract("forecast", result)
        return result
