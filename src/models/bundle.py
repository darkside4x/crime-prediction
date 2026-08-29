"""Safe loading of versioned estimator payloads."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import ModelConfig
from .contracts import validate_contract
from .errors import DataContractError, OptionalDependencyError
from .estimators import (
    CountEstimator,
    HistoricalRateEstimator,
    LightGBMEstimator,
    PreviousPeriodEstimator,
    RegularizedPoissonEstimator,
)
from .provenance import sha256_file


def _decode_key(value: str) -> tuple[str, ...]:
    return tuple(value.split("|"))


def _from_json_parameters(payload: dict, config: ModelConfig) -> CountEstimator:
    name = payload.get("estimator")
    if name == "previous_period":
        return PreviousPeriodEstimator()
    if name == "historical_rate":
        estimator = HistoricalRateEstimator(payload["target"])
        estimator._global = float(payload["global_mean"])
        estimator._category = {key: float(value) for key, value in payload["category_means"].items()}
        estimator._cell_category = {
            _decode_key(key): float(value) for key, value in payload["cell_category_means"].items()
        }
        estimator._exact = {
            (parts[0], parts[1], int(parts[2]), int(parts[3])): float(value)
            for key, value in payload["comparable_bucket_means"].items()
            for parts in [_decode_key(key)]
        }
        return estimator
    if name == "regularized_poisson":
        if tuple(payload["features"]) != config.features:
            raise DataContractError("Poisson payload feature order does not match configuration")
        estimator = RegularizedPoissonEstimator(config)
        estimator.mean_ = np.asarray(payload["feature_mean"], dtype=float)
        estimator.scale_ = np.asarray(payload["feature_scale"], dtype=float)
        estimator.coef_ = np.asarray(payload["coefficients"], dtype=float)
        return estimator
    raise DataContractError(f"Unsupported JSON estimator payload: {name!r}")


def load_estimator(
    bundle: dict, payload_path: str | Path, config: ModelConfig
) -> CountEstimator:
    """Validate integrity and load only allowlisted JSON or LightGBM text formats."""
    validate_contract("model-bundle", bundle)
    path = Path(payload_path)
    if sha256_file(path) != bundle["payload"]["sha256"]:
        raise DataContractError("Model payload checksum does not match bundle metadata")
    if tuple(bundle["features"]) != config.features:
        raise DataContractError("Model bundle feature order does not match configuration")
    if bundle["serializer"] == "json_parameters":
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        estimator = _from_json_parameters(wrapper["estimator"], config)
    elif bundle["serializer"] == "lightgbm_text":
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise OptionalDependencyError("Loading a LightGBM bundle requires lightgbm") from exc
        estimator = LightGBMEstimator(config)
        estimator.booster = lgb.Booster(model_file=str(path))
    else:
        raise DataContractError(f"Unsupported model serializer: {bundle['serializer']!r}")
    if estimator.name != bundle["estimator"]:
        raise DataContractError("Payload estimator does not match bundle metadata")
    return estimator
