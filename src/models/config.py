"""Validated experiment configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path

from .errors import ConfigurationError


DEFAULT_FEATURES = (
    "lag_1",
    "lag_2",
    "lag_7",
    "lag_14",
    "rolling_7_mean",
    "rolling_14_mean",
    "neighbor_lag_1",
    "recent_trend",
    "hour_sin",
    "hour_cos",
    "day_of_week_sin",
    "day_of_week_cos",
    "coverage_ratio",
)


@dataclass(frozen=True)
class ModelConfig:
    """Settings which fully determine a modeling run."""

    schema_version: str = "1.0.0"
    experiment_name: str = "aggregate-count-baseline"
    target: str = "event_count"
    window_hours: int = 6
    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    min_intervals_per_split: int = 3
    poisson_l2: float = 1.0
    poisson_max_iterations: int = 100
    poisson_tolerance: float = 1e-7
    selection_min_relative_gain: float = 0.01
    primary_metric: str = "poisson_deviance"
    top_k_fraction: float = 0.1
    calibration_bins: int = 10
    min_training_events_to_publish: int = 3
    risk_band_thresholds: tuple[float, float, float] = (0.2, 0.5, 0.75)
    enable_lightgbm: bool = True
    random_seed: int = 20260829
    rolling_origins: int = 3
    bootstrap_samples: int = 200
    explicit_train_end: str | None = None
    explicit_validation_end: str | None = None
    features: tuple[str, ...] = field(default_factory=lambda: DEFAULT_FEATURES)

    @classmethod
    def from_path(cls, path: str | Path) -> "ModelConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if "features" in payload:
            payload["features"] = tuple(payload["features"])
        if "risk_band_thresholds" in payload:
            payload["risk_band_thresholds"] = tuple(payload["risk_band_thresholds"])
        try:
            config = cls(**payload)
        except TypeError as exc:
            raise ConfigurationError(f"Invalid configuration field: {exc}") from exc
        config.validate()
        return config

    def validate(self) -> None:
        if self.schema_version != "1.0.0":
            raise ConfigurationError("Only model config schema_version 1.0.0 is supported")
        if self.target != "event_count":
            raise ConfigurationError("Only the aggregate count target 'event_count' is supported")
        if not 0 < self.train_fraction < 1 or not 0 < self.validation_fraction < 1:
            raise ConfigurationError("Split fractions must be between zero and one")
        if self.train_fraction + self.validation_fraction >= 1:
            raise ConfigurationError("Train and validation fractions must leave an untouched test block")
        if self.min_intervals_per_split < 1:
            raise ConfigurationError("min_intervals_per_split must be positive")
        if self.primary_metric != "poisson_deviance":
            raise ConfigurationError("The supported primary metric is poisson_deviance")
        if not 0 < self.top_k_fraction <= 1:
            raise ConfigurationError("top_k_fraction must be in (0, 1]")
        if self.calibration_bins < 2:
            raise ConfigurationError("calibration_bins must be at least two")
        if self.rolling_origins < 1:
            raise ConfigurationError("rolling_origins must be positive")
        if self.bootstrap_samples < 0:
            raise ConfigurationError("bootstrap_samples cannot be negative")
        if tuple(sorted(self.risk_band_thresholds)) != self.risk_band_thresholds:
            raise ConfigurationError("risk_band_thresholds must be ordered")
        if any(value <= 0 or value >= 1 for value in self.risk_band_thresholds):
            raise ConfigurationError("risk band thresholds must be inside (0, 1)")
        if not self.features:
            raise ConfigurationError("At least one feature is required")
        if (self.explicit_train_end is None) != (self.explicit_validation_end is None):
            raise ConfigurationError(
                "explicit_train_end and explicit_validation_end must be supplied together"
            )
        if self.explicit_train_end is not None:
            train_end = _parse_boundary(self.explicit_train_end)
            validation_end = _parse_boundary(self.explicit_validation_end or "")
            if train_end >= validation_end:
                raise ConfigurationError("explicit_train_end must precede explicit_validation_end")

    def to_dict(self) -> dict[str, object]:
        result = dict(self.__dict__)
        result["features"] = list(self.features)
        result["risk_band_thresholds"] = list(self.risk_band_thresholds)
        return result


def _parse_boundary(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid UTC split boundary: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ConfigurationError(f"Split boundary must be UTC: {value!r}")
    return parsed
