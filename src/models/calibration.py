"""Validation-only occurrence-probability calibration with safe JSON serialization."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np

from .errors import DataContractError


class IsotonicProbabilityCalibrator:
    """Small weighted pool-adjacent-violators calibrator.

    It uses only occurrence labels supplied to ``fit`` and serializes numeric
    thresholds rather than executable objects.
    """

    def __init__(self, thresholds: list[float] | None = None, values: list[float] | None = None) -> None:
        self.thresholds = thresholds or []
        self.values = values or []

    def fit(self, actual_counts: np.ndarray, raw_probability: np.ndarray) -> "IsotonicProbabilityCalibrator":
        truth = (np.asarray(actual_counts, dtype=float) > 0).astype(float)
        probability = np.clip(np.asarray(raw_probability, dtype=float), 0.0, 1.0)
        if truth.shape != probability.shape or truth.ndim != 1 or len(truth) < 2:
            raise DataContractError("Calibration requires aligned one-dimensional validation rows")
        order = np.argsort(probability, kind="stable")
        x = probability[order]
        y = truth[order]
        blocks: list[dict[str, float]] = []
        for point, label in zip(x, y, strict=True):
            blocks.append({"upper": float(point), "sum": float(label), "weight": 1.0})
            while len(blocks) >= 2:
                left, right = blocks[-2], blocks[-1]
                if left["sum"] / left["weight"] <= right["sum"] / right["weight"]:
                    break
                blocks[-2:] = [{
                    "upper": right["upper"],
                    "sum": left["sum"] + right["sum"],
                    "weight": left["weight"] + right["weight"],
                }]
        self.thresholds = [block["upper"] for block in blocks]
        self.values = [block["sum"] / block["weight"] for block in blocks]
        return self

    def predict(self, raw_probability: np.ndarray | float) -> np.ndarray:
        if not self.thresholds or len(self.thresholds) != len(self.values):
            raise DataContractError("Probability calibrator is not fitted")
        values = np.clip(np.asarray(raw_probability, dtype=float), 0.0, 1.0)
        indices = np.searchsorted(np.asarray(self.thresholds), values, side="left")
        indices = np.minimum(indices, len(self.values) - 1)
        return np.asarray(self.values, dtype=float)[indices]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "method": "validation_isotonic_pav_v1",
            "thresholds": self.thresholds,
            "values": self.values,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {"calibration_version": f"isotonic-{digest[:16]}", **payload}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IsotonicProbabilityCalibrator":
        if payload.get("method") != "validation_isotonic_pav_v1":
            raise DataContractError("Unsupported calibration method")
        thresholds = payload.get("thresholds")
        values = payload.get("values")
        if not isinstance(thresholds, list) or not isinstance(values, list) or not thresholds:
            raise DataContractError("Calibration artifact is invalid")
        if len(thresholds) != len(values):
            raise DataContractError("Calibration artifact lengths do not match")
        parsed_thresholds = [float(value) for value in thresholds]
        parsed_values = [float(value) for value in values]
        if (
            any(not math.isfinite(value) or not 0 <= value <= 1 for value in parsed_thresholds)
            or parsed_thresholds != sorted(parsed_thresholds)
            or any(
            not math.isfinite(value) or not 0 <= value <= 1 for value in parsed_values
            )
            or parsed_values != sorted(parsed_values)
        ):
            raise DataContractError("Calibration artifact values are invalid")
        restored = cls(parsed_thresholds, parsed_values)
        declared_version = payload.get("calibration_version")
        if declared_version is not None and declared_version != restored.to_dict()["calibration_version"]:
            raise DataContractError("Calibration artifact version does not match its contents")
        return restored
