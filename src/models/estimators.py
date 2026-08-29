"""Count estimators ordered from simplest to most complex."""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Protocol

import numpy as np

from .config import ModelConfig
from .data import design_matrix, target_vector
from .errors import OptionalDependencyError


class CountEstimator(Protocol):
    name: str

    def fit(self, rows: list[dict]) -> "CountEstimator": ...
    def predict(self, rows: list[dict]) -> np.ndarray: ...
    def drivers(self, row: dict, limit: int = 5) -> list[dict[str, str]]: ...
    def to_bundle(self) -> dict: ...


class PreviousPeriodEstimator:
    """Naive baseline that predicts the immediately preceding aggregate count."""

    name = "previous_period"

    def fit(self, rows: list[dict]) -> "PreviousPeriodEstimator":
        return self

    def predict(self, rows: list[dict]) -> np.ndarray:
        return np.asarray([row["lag_1"] for row in rows], dtype=float)

    def drivers(self, row: dict, limit: int = 5) -> list[dict[str, str]]:
        return [{"feature": "lag_1", "direction": "higher"}]

    def to_bundle(self) -> dict:
        return {"estimator": self.name, "target": "event_count", "feature": "lag_1"}


class HistoricalRateEstimator:
    """Hierarchical historical mean with comparable UTC time buckets."""

    name = "historical_rate"

    def __init__(self, target: str = "event_count") -> None:
        self.target = target
        self._exact: dict[tuple[str, str, int, int], float] = {}
        self._cell_category: dict[tuple[str, str], float] = {}
        self._category: dict[str, float] = {}
        self._global = 0.0

    @staticmethod
    def _mean_map(values: dict[object, list[float]]) -> dict[object, float]:
        return {key: float(np.mean(items)) for key, items in values.items()}

    def fit(self, rows: list[dict]) -> "HistoricalRateEstimator":
        exact: dict[tuple, list[float]] = defaultdict(list)
        cell_category: dict[tuple, list[float]] = defaultdict(list)
        category: dict[str, list[float]] = defaultdict(list)
        all_values = []
        for row in rows:
            timestamp = row["interval_start"]
            value = float(row[self.target])
            exact[(row["cell_id"], row["category"], timestamp.weekday(), timestamp.hour)].append(value)
            cell_category[(row["cell_id"], row["category"])].append(value)
            category[row["category"]].append(value)
            all_values.append(value)
        self._exact = self._mean_map(exact)
        self._cell_category = self._mean_map(cell_category)
        self._category = self._mean_map(category)
        self._global = float(np.mean(all_values))
        return self

    def predict(self, rows: list[dict]) -> np.ndarray:
        predictions = []
        for row in rows:
            timestamp = row["interval_start"]
            exact_key = (row["cell_id"], row["category"], timestamp.weekday(), timestamp.hour)
            cell_key = (row["cell_id"], row["category"])
            predictions.append(
                self._exact.get(
                    exact_key,
                    self._cell_category.get(
                        cell_key, self._category.get(row["category"], self._global)
                    ),
                )
            )
        return np.asarray(predictions, dtype=float)

    def drivers(self, row: dict, limit: int = 5) -> list[dict[str, str]]:
        return [{"feature": "historical comparable-window rate", "direction": "higher"}]

    def to_bundle(self) -> dict:
        def encode_key(key: tuple) -> str:
            return "|".join(str(value) for value in key)

        return {
            "estimator": self.name,
            "target": self.target,
            "global_mean": self._global,
            "category_means": self._category,
            "cell_category_means": {
                encode_key(key): value for key, value in self._cell_category.items()
            },
            "comparable_bucket_means": {
                encode_key(key): value for key, value in self._exact.items()
            },
        }


class RegularizedPoissonEstimator:
    """Deterministic L2-regularized Poisson GLM fitted with Newton steps."""

    name = "regularized_poisson"

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.mean_: np.ndarray | None = None
        self.scale_: np.ndarray | None = None
        self.coef_: np.ndarray | None = None

    def fit(self, rows: list[dict]) -> "RegularizedPoissonEstimator":
        raw = design_matrix(rows, self.config.features)
        target = target_vector(rows, self.config.target)
        self.mean_ = raw.mean(axis=0)
        self.scale_ = raw.std(axis=0)
        self.scale_[self.scale_ < 1e-12] = 1.0
        standardized = (raw - self.mean_) / self.scale_
        matrix = np.column_stack((np.ones(len(rows)), standardized))
        beta = np.zeros(matrix.shape[1], dtype=float)
        beta[0] = math.log(max(float(target.mean()), 1e-6))
        penalty = np.eye(matrix.shape[1]) * self.config.poisson_l2
        penalty[0, 0] = 0.0
        for _ in range(self.config.poisson_max_iterations):
            eta = np.clip(matrix @ beta, -20.0, 20.0)
            mu = np.exp(eta)
            gradient = matrix.T @ (target - mu) - penalty @ beta
            hessian = (matrix.T * mu) @ matrix + penalty
            try:
                step = np.linalg.solve(hessian, gradient)
            except np.linalg.LinAlgError:
                step = np.linalg.pinv(hessian) @ gradient
            beta_next = beta + step
            if float(np.max(np.abs(step))) < self.config.poisson_tolerance:
                beta = beta_next
                break
            beta = beta_next
        self.coef_ = beta
        return self

    def _require_fit(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if self.mean_ is None or self.scale_ is None or self.coef_ is None:
            raise RuntimeError("Estimator must be fitted before prediction")
        return self.mean_, self.scale_, self.coef_

    def predict(self, rows: list[dict]) -> np.ndarray:
        mean, scale, coefficients = self._require_fit()
        raw = design_matrix(rows, self.config.features)
        matrix = np.column_stack((np.ones(len(rows)), (raw - mean) / scale))
        return np.exp(np.clip(matrix @ coefficients, -20.0, 20.0))

    def drivers(self, row: dict, limit: int = 5) -> list[dict[str, str]]:
        mean, scale, coefficients = self._require_fit()
        raw = np.asarray([row[name] for name in self.config.features], dtype=float)
        contributions = ((raw - mean) / scale) * coefficients[1:]
        order = np.argsort(np.abs(contributions))[::-1][:limit]
        return [
            {
                "feature": self.config.features[index],
                "direction": "higher" if contributions[index] >= 0 else "lower",
            }
            for index in order
            if abs(contributions[index]) > 1e-12
        ]

    def to_bundle(self) -> dict:
        mean, scale, coefficients = self._require_fit()
        return {
            "estimator": self.name,
            "target": self.config.target,
            "features": list(self.config.features),
            "feature_mean": mean.tolist(),
            "feature_scale": scale.tolist(),
            "coefficients": coefficients.tolist(),
            "l2": self.config.poisson_l2,
        }


class LightGBMEstimator:
    """Optional LightGBM Poisson candidate using only declared numeric features."""

    name = "lightgbm_poisson"

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.model = None
        self.booster = None

    def fit(self, rows: list[dict]) -> "LightGBMEstimator":
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise OptionalDependencyError("LightGBM candidate requested but lightgbm is not installed") from exc
        self.model = lgb.LGBMRegressor(
            objective="poisson",
            n_estimators=200,
            learning_rate=0.04,
            num_leaves=15,
            min_child_samples=20,
            subsample=0.9,
            colsample_bytree=0.9,
            reg_lambda=1.0,
            random_state=self.config.random_seed,
            deterministic=True,
            verbosity=-1,
        )
        self.model.fit(
            design_matrix(rows, self.config.features),
            target_vector(rows, self.config.target),
        )
        self.booster = self.model.booster_
        return self

    def predict(self, rows: list[dict]) -> np.ndarray:
        if self.booster is None:
            raise RuntimeError("Estimator must be fitted before prediction")
        return np.maximum(
            self.booster.predict(design_matrix(rows, self.config.features)), 0.0
        )

    def drivers(self, row: dict, limit: int = 5) -> list[dict[str, str]]:
        if self.booster is None:
            raise RuntimeError("Estimator must be fitted before prediction")
        contributions = self.booster.predict(
            design_matrix([row], self.config.features), pred_contrib=True
        )[0][:-1]
        order = np.argsort(np.abs(contributions))[::-1][:limit]
        return [
            {
                "feature": self.config.features[index],
                "direction": "higher" if contributions[index] >= 0 else "lower",
            }
            for index in order
            if abs(contributions[index]) > 1e-12
        ]

    def to_bundle(self) -> dict:
        if self.booster is None:
            raise RuntimeError("Estimator must be fitted before serialization")
        return {
            "estimator": self.name,
            "target": self.config.target,
            "features": list(self.config.features),
            "lightgbm_model": self.booster.model_to_string(),
        }
