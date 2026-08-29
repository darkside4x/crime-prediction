"""Chronological splitting and rolling-origin folds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import ModelConfig, _parse_boundary
from .errors import DataContractError


@dataclass(frozen=True)
class ChronologicalSplit:
    train: list[dict]
    validation: list[dict]
    test: list[dict]
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime


def chronological_split(rows: list[dict], config: ModelConfig) -> ChronologicalSplit:
    times = sorted({row["interval_start"] for row in rows})
    minimum = config.min_intervals_per_split * 3
    if len(times) < minimum:
        raise DataContractError(
            f"Tenant has {len(times)} intervals; at least {minimum} are required for chronological splits"
        )
    if config.explicit_train_end is not None:
        train_boundary = _parse_boundary(config.explicit_train_end)
        validation_boundary = _parse_boundary(config.explicit_validation_end or "")
        train_times = {value for value in times if value <= train_boundary}
        validation_times = {
            value for value in times if train_boundary < value <= validation_boundary
        }
        test_times = {value for value in times if value > validation_boundary}
        if min(map(len, (train_times, validation_times, test_times))) < config.min_intervals_per_split:
            raise DataContractError("Explicit split boundaries leave an undersized chronological block")
    else:
        train_count = max(config.min_intervals_per_split, int(len(times) * config.train_fraction))
        validation_count = max(
            config.min_intervals_per_split, int(len(times) * config.validation_fraction)
        )
        if train_count + validation_count > len(times) - config.min_intervals_per_split:
            validation_count = len(times) - config.min_intervals_per_split - train_count
        if validation_count < config.min_intervals_per_split:
            raise DataContractError("Split fractions do not leave enough validation/test intervals")
        train_times = set(times[:train_count])
        validation_times = set(times[train_count : train_count + validation_count])
        test_times = set(times[train_count + validation_count :])
    return ChronologicalSplit(
        train=[row for row in rows if row["interval_start"] in train_times],
        validation=[row for row in rows if row["interval_start"] in validation_times],
        test=[row for row in rows if row["interval_start"] in test_times],
        train_start=min(train_times),
        train_end=max(train_times),
        validation_start=min(validation_times),
        validation_end=max(validation_times),
        test_start=min(test_times),
        test_end=max(test_times),
    )


def rolling_origin_folds(rows: list[dict], config: ModelConfig) -> list[tuple[list[dict], list[dict]]]:
    """Return expanding-window folds ending before the untouched test block."""
    base = chronological_split(rows, config)
    eligible = base.train + base.validation
    times = sorted({row["interval_start"] for row in eligible})
    validation_width = max(config.min_intervals_per_split, len(times) // (config.rolling_origins + 2))
    folds: list[tuple[list[dict], list[dict]]] = []
    for origin in range(config.rolling_origins, 0, -1):
        end = len(times) - (origin - 1) * validation_width
        start = end - validation_width
        if start < config.min_intervals_per_split:
            continue
        train_times = set(times[:start])
        validation_times = set(times[start:end])
        folds.append(
            (
                [row for row in eligible if row["interval_start"] in train_times],
                [row for row in eligible if row["interval_start"] in validation_times],
            )
        )
    return folds
