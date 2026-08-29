from __future__ import annotations

import numpy as np

from src.models.config import ModelConfig
from src.models.data import validate_rows
from src.models.estimators import (
    HistoricalRateEstimator,
    LightGBMEstimator,
    PreviousPeriodEstimator,
    RegularizedPoissonEstimator,
)
from src.models.split import chronological_split
from tests.models.helpers import TENANTS, synthetic_rows


def _split():
    config = ModelConfig(
        enable_lightgbm=False,
        train_fraction=0.5,
        validation_fraction=0.25,
        min_intervals_per_split=4,
        poisson_max_iterations=50,
    )
    return config, chronological_split(validate_rows(synthetic_rows(TENANTS[0]), config), config)


def test_baselines_and_poisson_emit_finite_nonnegative_counts() -> None:
    config, split = _split()
    estimators = [
        HistoricalRateEstimator().fit(split.train),
        PreviousPeriodEstimator().fit(split.train),
        RegularizedPoissonEstimator(config).fit(split.train),
    ]
    for estimator in estimators:
        predicted = estimator.predict(split.validation)
        assert len(predicted) == len(split.validation)
        assert np.isfinite(predicted).all()
        assert (predicted >= 0).all()


def test_lightgbm_candidate_and_directional_drivers() -> None:
    config, split = _split()
    estimator = LightGBMEstimator(config).fit(split.train)
    predicted = estimator.predict(split.validation)
    assert np.isfinite(predicted).all()
    assert (predicted >= 0).all()
    assert all(driver["direction"] in {"higher", "lower"} for driver in estimator.drivers(split.validation[0]))
