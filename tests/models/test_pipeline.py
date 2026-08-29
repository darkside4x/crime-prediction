from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace

import numpy as np
import pyarrow.parquet as pq
import pytest

from src.models.bundle import load_estimator
from src.models.contracts import validate_contract
from src.models.errors import DataContractError
from src.models.config import ModelConfig
from src.models.data import load_rows, validate_rows
from src.models.pipeline import _select_model, _train_and_compare, run_evaluation
from src.models.split import chronological_split
from tests.models.helpers import TENANTS, synthetic_rows, write_feature_manifest, write_test_config


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_two_tenant_pipeline_exports_isolated_schema_valid_artifacts(tmp_path: Path) -> None:
    manifests = [write_feature_manifest(tmp_path / "inputs", tenant) for tenant in TENANTS]
    config = write_test_config(tmp_path)
    output = tmp_path / "artifacts"
    results = run_evaluation(
        config_path=config,
        feature_manifest_paths=manifests,
        output_root=output,
    )
    assert {result["tenant_id"] for result in results} == set(TENANTS)
    model_config = ModelConfig.from_path(config)
    fact_ids_by_tenant = {}
    for result in results:
        tenant_id = result["tenant_id"]
        root = Path(result["run_manifest"]).parent
        assert f"tenant={tenant_id}" in str(root)
        run_manifest = _load(root / "run-manifest.json")
        evaluation = _load(root / "evaluation.json")
        model_card = _load(root / "model-card.json")
        bundle = _load(root / "bundle.json")
        facts = _load(root / "reka-facts.json")
        validate_contract("model-run-manifest", run_manifest)
        validate_contract("evaluation-report", evaluation)
        validate_contract("model-card", model_card)
        validate_contract("model-bundle", bundle)
        validate_contract("reka-fact-bundle", facts)
        assert model_card["training_period"]["end"] == run_manifest["split"]["test_end"]
        assert "model and temporal" in model_card["uncertainty_method"]
        calibration = _load(root / "calibration.json")
        uncertainty = _load(root / "uncertainty.json")
        assert calibration["fitted_on"] == "validation_only_pre_test_predictions"
        assert uncertainty["method"] == "rolling_origin_model_data_temporal_v1"
        assert set(uncertainty["components"]) == {
            "model_refit_variation",
            "temporal_residual_variation",
            "data_coverage_availability",
        }
        assert {payload["tenant_id"] for payload in (run_manifest, evaluation, model_card, bundle, facts)} == {tenant_id}
        fact_ids_by_tenant[tenant_id] = {fact["fact_id"] for fact in facts["facts"]}
        rows = pq.ParquetFile(root / "predictions.parquet").read().to_pylist()
        assert rows
        assert {row["tenant_id"] for row in rows} == {tenant_id}
        for row in rows:
            validate_contract("prediction", row)
            assert not {"latitude", "longitude", "external_event_id"}.intersection(row)
        input_rows = validate_rows(load_rows(run_manifest["input"]["path"]), model_config)
        test_rows = chronological_split(input_rows, model_config).test
        loaded = load_estimator(bundle, bundle["payload"]["path"], model_config)
        reproduced = loaded.predict(test_rows)
        assert len(reproduced) == len(test_rows)
        assert np.isfinite(reproduced).all()
    assert fact_ids_by_tenant[TENANTS[0]].isdisjoint(fact_ids_by_tenant[TENANTS[1]])


def test_fact_ids_are_stable_across_repeated_runs(tmp_path: Path) -> None:
    manifest = write_feature_manifest(tmp_path / "inputs", TENANTS[0])
    config = write_test_config(tmp_path)
    first = run_evaluation(config_path=config, feature_manifest_paths=[manifest], output_root=tmp_path / "first")[0]
    second = run_evaluation(config_path=config, feature_manifest_paths=[manifest], output_root=tmp_path / "second")[0]
    first_facts = _load(Path(first["run_manifest"]).parent / "reka-facts.json")
    second_facts = _load(Path(second["run_manifest"]).parent / "reka-facts.json")
    assert [fact["fact_id"] for fact in first_facts["facts"]] == [
        fact["fact_id"] for fact in second_facts["facts"]
    ]


def test_pipeline_rejects_cross_tenant_feature_artifact(tmp_path: Path) -> None:
    mixed = synthetic_rows(TENANTS[0]) + synthetic_rows(TENANTS[1])
    manifest = write_feature_manifest(tmp_path / "inputs", TENANTS[0], mixed)
    config = write_test_config(tmp_path)
    with pytest.raises(DataContractError, match="contains tenant set"):
        run_evaluation(
            config_path=config,
            feature_manifest_paths=[manifest],
            output_root=tmp_path / "artifacts",
        )


def test_model_selection_does_not_consult_test_targets() -> None:
    config = ModelConfig(
        enable_lightgbm=False,
        train_fraction=0.5,
        validation_fraction=0.25,
        min_intervals_per_split=4,
        rolling_origins=2,
        bootstrap_samples=0,
    )
    rows = validate_rows(synthetic_rows(TENANTS[0]), config)
    original = chronological_split(rows, config)
    _, _, original_metrics, _ = _train_and_compare(original, config)
    changed_test = [dict(row, event_count=row["event_count"] + 100) for row in original.test]
    modified = replace(original, test=changed_test)
    _, _, modified_metrics, _ = _train_and_compare(modified, config)
    assert _select_model(original_metrics, config) == _select_model(modified_metrics, config)


def test_low_support_predictions_and_reka_facts_are_suppressed(tmp_path: Path) -> None:
    manifest = write_feature_manifest(tmp_path / "inputs", TENANTS[0])
    config_path = write_test_config(tmp_path)
    config_payload = _load(config_path)
    config_payload["min_training_events_to_publish"] = 1000
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    result = run_evaluation(
        config_path=config_path,
        feature_manifest_paths=[manifest],
        output_root=tmp_path / "artifacts",
    )[0]
    root = Path(result["run_manifest"]).parent
    predictions = pq.ParquetFile(root / "predictions.parquet").read().to_pylist()
    assert all(row["suppressed"] for row in predictions)
    assert all(row["risk"] == 0 and row["expected_count"] == 0 for row in predictions)
    facts = _load(root / "reka-facts.json")
    mean_risk = next(fact for fact in facts["facts"] if fact["label"].startswith("Mean model-implied risk"))
    assert mean_risk["suppressed"] is True
    assert mean_risk["value"] is None
