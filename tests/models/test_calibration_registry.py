from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import pytest

from src.models.calibration import IsotonicProbabilityCalibrator
from src.models.config import ModelConfig
from src.models.metrics import top_k_capture, top_k_capture_per_window
from src.models.operational import ForecastService
from src.models.pipeline import run_evaluation
from src.models.registry import FilesystemApprovedModelRegistry
from src.models.errors import DataContractError

from .helpers import TENANTS, write_feature_manifest, write_test_config


def test_top_k_capture_is_computed_within_each_time_window() -> None:
    rows = [
        {"interval_start": "2026-01-01T00:00:00Z", "category": "property"},
        {"interval_start": "2026-01-01T00:00:00Z", "category": "property"},
        {"interval_start": "2026-01-01T06:00:00Z", "category": "property"},
        {"interval_start": "2026-01-01T06:00:00Z", "category": "property"},
    ]
    actual = np.asarray([10, 0, 1, 0], dtype=float)
    predicted = np.asarray([100, 99, 1, 0], dtype=float)
    assert top_k_capture(actual, predicted, 0.5) < 1.0
    assert top_k_capture_per_window(rows, actual, predicted, 0.5) == 1.0


def test_isotonic_calibration_is_monotonic_and_json_round_trips() -> None:
    calibrator = IsotonicProbabilityCalibrator().fit(
        np.asarray([0, 1, 0, 1, 1], dtype=float),
        np.asarray([0.1, 0.2, 0.4, 0.8, 0.9], dtype=float),
    )
    predicted = calibrator.predict(np.linspace(0, 1, 20))
    assert np.all(np.diff(predicted) >= 0)
    restored = IsotonicProbabilityCalibrator.from_dict(calibrator.to_dict())
    assert np.array_equal(predicted, restored.predict(np.linspace(0, 1, 20)))


def test_approved_registry_loads_final_refit_calibration_and_uncertainty(tmp_path: Path) -> None:
    tenant = TENANTS[0]
    manifest = write_feature_manifest(tmp_path / "inputs", tenant)
    config_path = write_test_config(tmp_path)
    output = tmp_path / "registry"
    result = run_evaluation(
        config_path=config_path,
        feature_manifest_paths=[manifest],
        output_root=output,
    )[0]
    registry = FilesystemApprovedModelRegistry(
        output, config=ModelConfig.from_path(config_path)
    )
    approval = registry.promote(
        tenant,
        result["model_version"],
        approved_by="operator-test",
        reason="Chronological evaluation reviewed",
    )
    assert approval["model_version"] == result["model_version"]
    approved = registry.approved_for(tenant)
    assert approved is not None
    assert approved.calibration_version.startswith("isotonic-")

    row = json.loads(Path("contracts/fixtures/forecast-feature-row.json").read_text())
    row.update(
        tenant_id=tenant,
        interval_start="2099-08-30T00:00:00Z",
        data_as_of="2099-08-29T23:59:59Z",
        coverage_ratio=0.9,
    )
    forecast = ForecastService(models=registry).forecast(
        row,
        tenant_id=tenant,
        generated_at=datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc),
    )
    assert forecast["model_version"] == result["model_version"]
    assert forecast["occurrence_probability"]["calibration_version"].startswith("isotonic-")
    assert forecast["expected_count"]["method"] == "rolling_origin_model_data_temporal_v1"

    model_dir = Path(result["run_manifest"]).parent
    calibration_path = model_dir / "calibration.json"
    calibration = json.loads(calibration_path.read_text())
    calibration["values"][0] = min(1.0, calibration["values"][0] + 0.01)
    calibration_path.write_text(json.dumps(calibration))
    restarted = FilesystemApprovedModelRegistry(
        output, config=ModelConfig.from_path(config_path)
    )
    with pytest.raises(DataContractError, match="calibration checksum"):
        restarted.approved_for(tenant)
