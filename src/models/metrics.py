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


def count_metrics(actual: np.ndarray, predicted: np.ndarray, top_k_fraction: float) -> dict[str, float]:
    truth = np.asarray(actual, dtype=float)
    prediction = np.maximum(np.asarray(predicted, dtype=float), 0.0)
    probability = 1.0 - np.exp(-prediction)
    occurred = (truth > 0).astype(float)
    return {
        "mae": float(np.mean(np.abs(truth - prediction))),
        "poisson_deviance": poisson_deviance(truth, prediction),
        "top_k_capture": top_k_capture(truth, prediction, top_k_fraction),
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
            metrics = count_metrics(actual, predicted[indices], top_k_fraction)
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
) -> tuple[float, float] | None:
    if samples <= 0 or len(actual) < 2:
        return None
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(samples):
        indices = rng.integers(0, len(actual), len(actual))
        values.append(count_metrics(actual[indices], predicted[indices], top_k_fraction)[metric_name])
    return float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))
