"""Deterministic probability calibration and residual uncertainty."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ProbabilityCalibrator:
    """Quantile-bin calibration for the probability of at least one event."""

    bins: int
    edges_: np.ndarray | None = None
    rates_: np.ndarray | None = None

    def fit(self, counts: np.ndarray, expected_counts: np.ndarray) -> "ProbabilityCalibrator":
        raw = 1.0 - np.exp(-np.maximum(expected_counts, 0.0))
        occurred = (counts > 0).astype(float)
        edges = np.unique(np.quantile(raw, np.linspace(0.0, 1.0, self.bins + 1)))
        if len(edges) < 2:
            edges = np.asarray([0.0, 1.0])
        rates = []
        global_rate = float(occurred.mean())
        for index in range(len(edges) - 1):
            mask = (raw >= edges[index]) & (
                raw <= edges[index + 1]
                if index == len(edges) - 2
                else raw < edges[index + 1]
            )
            # Beta(1,1) shrinkage prevents exact zero/one from tiny validation bins.
            positives = float(occurred[mask].sum()) if mask.any() else 0.0
            rate = (positives + global_rate * 2.0) / (float(mask.sum()) + 2.0)
            rates.append(rate)
        self.edges_ = edges
        self.rates_ = np.maximum.accumulate(np.asarray(rates, dtype=float))
        return self

    def predict(self, expected_counts: np.ndarray) -> np.ndarray:
        if self.edges_ is None or self.rates_ is None:
            raise RuntimeError("Calibrator must be fitted before prediction")
        raw = 1.0 - np.exp(-np.maximum(expected_counts, 0.0))
        indices = np.searchsorted(self.edges_[1:-1], raw, side="right")
        return np.clip(self.rates_[indices], 0.0, 1.0)

    def to_dict(self) -> dict[str, object]:
        if self.edges_ is None or self.rates_ is None:
            raise RuntimeError("Calibrator must be fitted before serialization")
        return {"method": "quantile_bin_beta_shrinkage", "edges": self.edges_.tolist(), "rates": self.rates_.tolist()}


@dataclass
class ResidualInterval:
    """Validation-residual interval applied to non-negative count predictions."""

    lower_quantile_: float | None = None
    upper_quantile_: float | None = None

    def fit(self, counts: np.ndarray, expected_counts: np.ndarray) -> "ResidualInterval":
        residuals = counts - expected_counts
        self.lower_quantile_ = float(np.quantile(residuals, 0.05))
        self.upper_quantile_ = float(np.quantile(residuals, 0.95))
        return self

    def predict(self, expected_counts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.lower_quantile_ is None or self.upper_quantile_ is None:
            raise RuntimeError("Residual interval must be fitted before prediction")
        return (
            np.maximum(expected_counts + self.lower_quantile_, 0.0),
            np.maximum(expected_counts + self.upper_quantile_, 0.0),
        )

    def to_dict(self) -> dict[str, object]:
        if self.lower_quantile_ is None or self.upper_quantile_ is None:
            raise RuntimeError("Residual interval must be fitted before serialization")
        return {"method": "validation_residual_quantiles", "lower_quantile": self.lower_quantile_, "upper_quantile": self.upper_quantile_, "coverage_target": 0.9}
