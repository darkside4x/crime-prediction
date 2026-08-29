"""Count, ranking, calibration, and slice metrics."""

from __future__ import annotations

from collections import defaultdict
import math

import numpy as np


def poisson_deviance(actual: np.ndarray, predicted: np.ndarray) -> float:
    prediction = np.maximum(np.asarray(predicted, dtype=float), 1e-12)
    truth = np.asarray(actual, dtype=float)
    terms = prediction.copy()
    positive = truth > 0
    terms[positive] = (
        truth[positive] * np.log(truth[positive] / prediction[positive])
        - truth[positive]
        + prediction[positive]
    )
    return float(2.0 * np.mean(terms))


def top_k_capture(actual: np.ndarray, predicted: np.ndarray, fraction: float) -> float:
    truth = np.asarray(actual, dtype=float)
    total = float(truth.sum())
    if total <= 0:
        return 0.0
    count = max(1, math.ceil(len(truth) * fraction))
    selected = np.argsort(np.asarray(predicted, dtype=float))[::-1][:count]
    return float(truth[selected].sum() / total)


def top_k_capture_per_window(
    rows: list[dict], actual: np.ndarray, predicted: np.ndarray, fraction: float
) -> float:
    """Capture computed inside every forecast window/category cell ranking."""
    if len(rows) != len(actual) or len(rows) != len(predicted):
        raise ValueError("Rows and prediction arrays must have equal length")
    grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        interval = row["interval_start"]
        key = interval.isoformat() if hasattr(interval, "isoformat") else str(interval)
        grouped[(key, str(row.get("category", "")))].append(index)
    values = []
    weights = []
    truth = np.asarray(actual, dtype=float)
    estimate = np.asarray(predicted, dtype=float)
    for indices in grouped.values():
        window_truth = truth[indices]
        total = float(window_truth.sum())
        if total <= 0:
            continue
        values.append(top_k_capture(window_truth, estimate[indices], fraction))
        weights.append(total)
    if not values:
        return 0.0
    return float(np.average(np.asarray(values), weights=np.asarray(weights)))


def count_metrics(
    actual: np.ndarray,
    predicted: np.ndarray,
    top_k_fraction: float,
    rows: list[dict] | None = None,
    occurrence_probability: np.ndarray | None = None,
) -> dict[str, float]:
    truth = np.asarray(actual, dtype=float)
    prediction = np.maximum(np.asarray(predicted, dtype=float), 0.0)
    probability = (
        1.0 - np.exp(-prediction)
        if occurrence_probability is None
        else np.clip(np.asarray(occurrence_probability, dtype=float), 0.0, 1.0)
    )
    occurred = (truth > 0).astype(float)
    return {
        "mae": float(np.mean(np.abs(truth - prediction))),
        "poisson_deviance": poisson_deviance(truth, prediction),
        "top_k_capture": (
            top_k_capture_per_window(rows, truth, prediction, top_k_fraction)
            if rows is not None
            else top_k_capture(truth, prediction, top_k_fraction)
        ),
        "brier_score": float(np.mean((occurred - probability) ** 2)),
    }


def calibration_table(actual: np.ndarray, predicted: np.ndarray, bins: int) -> list[dict]:
    probability = 1.0 - np.exp(-np.maximum(np.asarray(predicted, dtype=float), 0.0))
    return probability_calibration_table(actual, probability, bins)


def probability_calibration_table(
    actual: np.ndarray, probability: np.ndarray, bins: int
) -> list[dict]:
    probability = np.clip(np.asarray(probability, dtype=float), 0.0, 1.0)
    occurred = (np.asarray(actual, dtype=float) > 0).astype(float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    output = []
    for index in range(bins):
        upper_inclusive = index == bins - 1
        mask = (probability >= edges[index]) & (
            probability <= edges[index + 1] if upper_inclusive else probability < edges[index + 1]
        )
        if not mask.any():
            continue
        output.append(
            {
                "bin_lower": float(edges[index]),
                "bin_upper": float(edges[index + 1]),
                "rows": int(mask.sum()),
                "mean_predicted_probability": float(probability[mask].mean()),
                "observed_event_rate": float(occurred[mask].mean()),
            }
        )
    return output


def sliced_metrics(rows: list[dict], predicted: np.ndarray, top_k_fraction: float) -> dict[str, list[dict]]:
    dimensions = {
        "category": lambda row: row["category"],
        "time_of_day_utc": lambda row: f"{row['interval_start'].hour:02d}:00",
        "cell": lambda row: row["cell_id"],
        "coverage_band": lambda row: (
            "low" if row["coverage_ratio"] < 0.5 else "medium" if row["coverage_ratio"] < 0.9 else "high"
        ),
    }
    result: dict[str, list[dict]] = {}
    for dimension, key_function in dimensions.items():
        grouped: dict[str, list[int]] = defaultdict(list)
        for index, row in enumerate(rows):
            grouped[str(key_function(row))].append(index)
        entries = []
        for value, indices in sorted(grouped.items()):
            actual = np.asarray([rows[index]["event_count"] for index in indices], dtype=float)
            slice_rows = [rows[index] for index in indices]
            metrics = count_metrics(actual, predicted[indices], top_k_fraction, slice_rows)
            entries.append({"value": value, "row_count": len(indices), **metrics})
        result[dimension] = entries
    return result


def bootstrap_interval(
    actual: np.ndarray,
    predicted: np.ndarray,
    metric_name: str,
    top_k_fraction: float,
    samples: int,
    seed: int,
    rows: list[dict] | None = None,
    occurrence_probability: np.ndarray | None = None,
) -> tuple[float, float] | None:
    if samples <= 0 or len(actual) < 2:
        return None
    rng = np.random.default_rng(seed)
    values = []
    window_groups: list[list[int]] | None = None
    if rows is not None:
        grouped: dict[str, list[int]] = defaultdict(list)
        for index, row in enumerate(rows):
            interval = row["interval_start"]
            key = interval.isoformat() if hasattr(interval, "isoformat") else str(interval)
            grouped[key].append(index)
        window_groups = list(grouped.values())
    for _ in range(samples):
        if window_groups:
            selected_groups = rng.integers(0, len(window_groups), len(window_groups))
            indices = np.asarray(
                [index for group in selected_groups for index in window_groups[int(group)]],
                dtype=int,
            )
            sampled_rows = [rows[index] for index in indices] if rows is not None else None
        else:
            indices = rng.integers(0, len(actual), len(actual))
            sampled_rows = None
        values.append(
            count_metrics(
                actual[indices],
                predicted[indices],
                top_k_fraction,
                sampled_rows,
                occurrence_probability[indices] if occurrence_probability is not None else None,
            )[metric_name]
        )
    return float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))
