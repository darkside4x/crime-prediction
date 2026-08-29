from __future__ import annotations

from copy import deepcopy

import pytest

from src.models.config import ModelConfig
from src.models.data import validate_rows
from src.models.errors import DataContractError
from src.models.split import chronological_split
from tests.models.helpers import TENANTS, synthetic_rows


def test_feature_validation_rejects_future_availability_and_restricted_fields() -> None:
    config = ModelConfig(enable_lightgbm=False, min_intervals_per_split=2)
    row = synthetic_rows(TENANTS[0], intervals=1)[0]
    row["data_as_of"] = row["interval_start"]
    with pytest.raises(DataContractError, match="strictly prior"):
        validate_rows([row], config)

    row = synthetic_rows(TENANTS[0], intervals=1)[0]
    row["latitude"] = 12.9
    with pytest.raises(DataContractError, match="restricted"):
        validate_rows([row], config)


def test_feature_validation_rejects_duplicate_prediction_key() -> None:
    config = ModelConfig(enable_lightgbm=False, min_intervals_per_split=2)
    row = synthetic_rows(TENANTS[0], intervals=1)[0]
    with pytest.raises(DataContractError, match="Duplicate prediction key"):
        validate_rows([row, deepcopy(row)], config)


def test_split_is_strictly_chronological() -> None:
    config = ModelConfig(
        enable_lightgbm=False,
        train_fraction=0.5,
        validation_fraction=0.25,
        min_intervals_per_split=4,
    )
    rows = validate_rows(synthetic_rows(TENANTS[0]), config)
    split = chronological_split(rows, config)
    assert split.train_end < split.validation_start
    assert split.validation_end < split.test_start
    assert {row["interval_start"] for row in split.train}.isdisjoint(
        row["interval_start"] for row in split.test
    )
