"""End-to-end tenant-scoped training, evaluation, and artifact export."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import json
from pathlib import Path
import platform
from typing import Any, Callable

import numpy as np

from .calibration import ProbabilityCalibrator, ResidualInterval
from .config import ModelConfig
from .contracts import REPOSITORY_ROOT, validate_contract
from .data import load_rows, target_vector, validate_rows
from .errors import DataContractError, OptionalDependencyError
from .estimators import (
    CountEstimator,
    HistoricalRateEstimator,
    LightGBMEstimator,
    PreviousPeriodEstimator,
    RegularizedPoissonEstimator,
)
from .facts import build_fact_bundle
from .metrics import (
    bootstrap_interval,
    count_metrics,
    probability_calibration_table,
    sliced_metrics,
)
from .provenance import (
    code_version,
    display_path,
    format_utc,
    sha256_file,
    stable_hash,
    stable_uuid,
    utc_now,
    write_json,
)
from .split import ChronologicalSplit, chronological_split, rolling_origin_folds


METRIC_DEFINITIONS = {
    "mae": "Mean absolute aggregate count error; lower is better.",
    "poisson_deviance": "Mean Poisson deviance for aggregate counts; lower is better.",
    "top_k_capture": "Share of observed incidents captured by the highest-ranked configured fraction of aggregate rows; higher is better.",
    "brier_score": "Mean squared error of the probability of at least one aggregate event; lower is better.",
}


def _candidate_factories(config: ModelConfig) -> list[tuple[str, Callable[[], CountEstimator]]]:
    factories: list[tuple[str, Callable[[], CountEstimator]]] = [
        ("historical_rate", lambda: HistoricalRateEstimator(config.target)),
        ("previous_period", PreviousPeriodEstimator),
        ("regularized_poisson", lambda: RegularizedPoissonEstimator(config)),
    ]
    if config.enable_lightgbm:
        factories.append(("lightgbm_poisson", lambda: LightGBMEstimator(config)))
    return factories


def _metric_entries(
    model_name: str,
    split_name: str,
    values: dict[str, float],
    confidence: dict[str, tuple[float, float] | None] | None = None,
) -> list[dict[str, Any]]:
    entries = []
    for name, value in values.items():
        entry: dict[str, Any] = {
            "model": model_name,
            "split": split_name,
            "name": name,
            "value": float(value),
            "definition": METRIC_DEFINITIONS[name],
        }
        interval = confidence.get(name) if confidence else None
        if interval is not None:
            entry["ci_lower"], entry["ci_upper"] = interval
        entries.append(entry)
    return entries


def _select_model(
    validation_metrics: dict[str, dict[str, float]], config: ModelConfig
) -> tuple[str, float, str]:
    baseline = validation_metrics["historical_rate"][config.primary_metric]
    best_name = "historical_rate"
    best_value = baseline
    for name, metrics in validation_metrics.items():
        value = metrics[config.primary_metric]
        gain = (baseline - value) / baseline if baseline > 0 else 0.0
        if gain >= config.selection_min_relative_gain and value < best_value:
            best_name, best_value = name, value
    observed_gain = (baseline - best_value) / baseline if baseline > 0 else 0.0
    if best_name == "historical_rate":
        reason = (
            "No candidate achieved the configured validation improvement over the "
            "historical-rate baseline, so the simpler baseline was retained."
        )
    else:
        reason = (
            f"{best_name} reduced validation {config.primary_metric} by "
            f"{observed_gain:.2%}, exceeding the configured "
            f"{config.selection_min_relative_gain:.2%} threshold."
        )
    return best_name, float(observed_gain), reason


def _resolve_feature_artifact(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    declared = Path(manifest["artifact"]["path"])
    candidates = [declared] if declared.is_absolute() else [REPOSITORY_ROOT / declared, manifest_path.parent / declared]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise DataContractError(f"Feature artifact declared by {manifest_path} does not exist")


def _read_manifest(path: str | Path) -> tuple[Path, dict[str, Any], Path]:
    manifest_path = Path(path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_contract("feature-table-manifest", manifest)
    artifact_path = _resolve_feature_artifact(manifest_path, manifest)
    actual_checksum = sha256_file(artifact_path)
    if actual_checksum != manifest["artifact"]["sha256"]:
        raise DataContractError(
            f"Feature artifact checksum mismatch for tenant {manifest['tenant_id']}"
        )
    return manifest_path, manifest, artifact_path


def _train_and_compare(
    split: ChronologicalSplit, config: ModelConfig
) -> tuple[
    dict[str, CountEstimator],
    dict[str, np.ndarray],
    dict[str, dict[str, float]],
    list[dict[str, Any]],
]:
    models: dict[str, CountEstimator] = {}
    predictions: dict[str, np.ndarray] = {}
    validation_metrics: dict[str, dict[str, float]] = {}
    report_metrics: list[dict[str, Any]] = []
    actual = target_vector(split.validation, config.target)
    factories = _candidate_factories(config)
    for name, factory in factories:
        model = factory().fit(split.train)
        predicted = model.predict(split.validation)
        values = count_metrics(actual, predicted, config.top_k_fraction)
        models[name] = model
        predictions[name] = predicted
        validation_metrics[name] = values
        report_metrics.extend(_metric_entries(name, "validation", values))

    rolling_values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for fold_train, fold_validation in rolling_origin_folds(split.train + split.validation + split.test, config):
        fold_actual = target_vector(fold_validation, config.target)
        for name, factory in factories:
            fold_model = factory().fit(fold_train)
            values = count_metrics(
                fold_actual, fold_model.predict(fold_validation), config.top_k_fraction
            )
            for metric_name, value in values.items():
                rolling_values[name][metric_name].append(value)
    for name, by_metric in rolling_values.items():
        averaged = {metric_name: float(np.mean(values)) for metric_name, values in by_metric.items()}
        report_metrics.extend(_metric_entries(name, "rolling_origin", averaged))
    return models, predictions, validation_metrics, report_metrics


def _risk_band(value: float, thresholds: tuple[float, float, float]) -> str:
    if value < thresholds[0]:
        return "low"
    if value < thresholds[1]:
        return "typical"
    if value < thresholds[2]:
        return "elevated"
    return "high"


def _prediction_rows(
    *,
    rows: list[dict[str, Any]],
    expected: np.ndarray,
    risks: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    estimator: CountEstimator,
    training_rows: list[dict[str, Any]],
    tenant_id: str,
    model_version: str,
    data_version: str,
    data_as_of: str,
    config: ModelConfig,
) -> list[dict[str, Any]]:
    support: dict[tuple[str, str], float] = defaultdict(float)
    for row in training_rows:
        support[(row["cell_id"], row["category"])] += row[config.target]
    output = []
    for index, row in enumerate(rows):
        suppressed = support[(row["cell_id"], row["category"])] < config.min_training_events_to_publish
        prediction = {
            "schema_version": "2.0.0",
            "tenant_id": tenant_id,
            "cell_id": row["cell_id"],
            "window_start": format_utc(row["interval_start"]),
            "window_end": format_utc(row["interval_start"] + timedelta(hours=config.window_hours)),
            "category": row["category"],
            "risk": 0.0 if suppressed else float(np.clip(risks[index], 0.0, 1.0)),
            "risk_band": "low" if suppressed else _risk_band(float(risks[index]), config.risk_band_thresholds),
            "expected_count": 0.0 if suppressed else float(max(expected[index], 0.0)),
            "uncertainty": {
                "lower": 0.0 if suppressed else float(max(lower[index], 0.0)),
                "upper": 0.0 if suppressed else float(max(upper[index], 0.0)),
            },
            "drivers": [] if suppressed else estimator.drivers(row),
            "model_version": model_version,
            "data_version": data_version,
            "data_as_of": data_as_of,
            "suppressed": suppressed,
        }
        validate_contract("prediction", prediction)
        output.append(prediction)
    return output


def _evaluation_report(
    *,
    tenant_id: str,
    model_version: str,
    data_version: str,
    generated_at: str,
    selected_name: str,
    observed_gain: float,
    reason: str,
    report_metrics: list[dict[str, Any]],
    selected_model: CountEstimator,
    baseline_model: CountEstimator,
    split: ChronologicalSplit,
    config: ModelConfig,
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    actual = target_vector(split.test, config.target)
    selected_prediction = selected_model.predict(split.test)
    baseline_prediction = baseline_model.predict(split.test)
    test_models = {selected_name: selected_prediction}
    if selected_name != "historical_rate":
        test_models["historical_rate"] = baseline_prediction
    for offset, (name, predicted) in enumerate(test_models.items()):
        values = count_metrics(actual, predicted, config.top_k_fraction)
        confidence = {
            metric_name: bootstrap_interval(
                actual,
                predicted,
                metric_name,
                config.top_k_fraction,
                config.bootstrap_samples,
                config.random_seed + offset,
            )
            for metric_name in values
        }
        report_metrics.extend(_metric_entries(name, "test", values, confidence))

    validation_expected = selected_model.predict(split.validation)
    calibrator = ProbabilityCalibrator(config.calibration_bins).fit(
        target_vector(split.validation, config.target), validation_expected
    )
    calibration_entries = []
    for split_name, rows, expected in (
        ("validation", split.validation, validation_expected),
        ("test", split.test, selected_prediction),
    ):
        for entry in probability_calibration_table(
            target_vector(rows, config.target), calibrator.predict(expected), config.calibration_bins
        ):
            calibration_entries.append(
                {
                    "model": selected_name,
                    "split": split_name,
                    "bin_lower": entry["bin_lower"],
                    "bin_upper": entry["bin_upper"],
                    "row_count": entry["rows"],
                    "mean_predicted_probability": entry["mean_predicted_probability"],
                    "observed_event_rate": entry["observed_event_rate"],
                }
            )

    sliced = sliced_metrics(split.test, selected_prediction, config.top_k_fraction)
    slice_entries = []
    for dimension, entries in sliced.items():
        for entry in entries:
            slice_entries.append(
                {
                    "dimension": dimension,
                    "value": entry["value"],
                    "row_count": entry["row_count"],
                    "metrics": [
                        {
                            "name": name,
                            "value": entry[name],
                            "definition": METRIC_DEFINITIONS[name],
                        }
                        for name in ("mae", "poisson_deviance", "top_k_capture", "brier_score")
                    ],
                }
            )
    cell_slices = sorted(
        (entry for entry in slice_entries if entry["dimension"] == "cell"),
        key=lambda entry: next(metric["value"] for metric in entry["metrics"] if metric["name"] == "mae"),
        reverse=True,
    )[:20]
    report = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "evaluation_id": stable_uuid(tenant_id, model_version, "evaluation"),
        "model_version": model_version,
        "data_version": data_version,
        "generated_at": generated_at,
        "target": "next_window_count",
        "primary_metric": config.primary_metric,
        "selection": {
            "selected_model": selected_name,
            "baseline_model": "historical_rate",
            "selection_split": "validation",
            "minimum_relative_gain": config.selection_min_relative_gain,
            "observed_relative_gain": observed_gain,
            "reason": reason,
            "test_evaluated_after_selection": True,
        },
        "metrics": report_metrics,
        "calibration": calibration_entries,
        "slices": slice_entries,
        "spatial_displacement": {
            "definition": "Cells ranked by held-out mean absolute error; this is an aggregate model audit and not an enforcement recommendation.",
            "highest_error_cells": [
                {
                    "cell_id": entry["value"],
                    "row_count": entry["row_count"],
                    "mae": next(metric["value"] for metric in entry["metrics"] if metric["name"] == "mae"),
                }
                for entry in cell_slices
            ],
        },
        "limitations": [
            "Incident reports are an incomplete proxy for underlying harm and may reflect reporting or enforcement intensity.",
            "Drivers are model associations, not causal explanations.",
            "Performance may shift across time, categories, coverage levels, and geographies.",
            "This aggregate forecast must not be used for individual assessment or automated enforcement decisions.",
        ],
    }
    validate_contract("evaluation-report", report)
    return report, selected_prediction, calibrator.predict(selected_prediction)


def _model_card(
    report: dict[str, Any], split: ChronologicalSplit, config: ModelConfig
) -> dict[str, Any]:
    selected_name = report["selection"]["selected_model"]
    metric_name = config.primary_metric
    selected_value = next(
        item["value"]
        for item in report["metrics"]
        if item["model"] == selected_name and item["split"] == "test" and item["name"] == metric_name
    )
    baseline_value = next(
        item["value"]
        for item in report["metrics"]
        if item["model"] == "historical_rate" and item["split"] == "test" and item["name"] == metric_name
    )
    relative_gain = (baseline_value - selected_value) / baseline_value if baseline_value > 0 else 0.0
    card = {
        "schema_version": "1.0.0",
        "tenant_id": report["tenant_id"],
        "model_version": report["model_version"],
        "data_version": report["data_version"],
        "generated_at": report["generated_at"],
        "model_name": selected_name,
        "target": "next_window_count",
        "prediction_unit": f"Expected aggregate incident count for one tenant, H3 cell, category, and {config.window_hours}-hour UTC window.",
        "training_period": {"start": format_utc(split.train_start), "end": format_utc(split.train_end)},
        "evaluation_period": {"start": format_utc(split.test_start), "end": format_utc(split.test_end)},
        "primary_metric": {
            "name": metric_name,
            "value": selected_value,
            "split": "test",
            "definition": METRIC_DEFINITIONS[metric_name],
        },
        "baseline_comparison": {
            "baseline_model": "historical_rate",
            "baseline_value": baseline_value,
            "selected_value": selected_value,
            "relative_gain": relative_gain,
            "selected_model_beats_baseline": selected_value < baseline_value,
        },
        "intended_uses": [
            "Aggregate area-level incident-volume forecasting for planning and model evaluation with human interpretation."
        ],
        "prohibited_uses": [
            "Individual criminality scoring, suspect identification, victim-address views, or protected-attribute inference.",
            "Automated patrol, enforcement, detention, investigation, or resource-allocation recommendations.",
            "Treating model drivers as causes or forecasts as observed ground truth.",
        ],
        "limitations": list(report["limitations"]),
        "uncertainty_method": "A 90% interval is formed from the 5th and 95th percentiles of chronological validation residuals and clipped at zero.",
        "suppression_policy": f"Cell/category outputs with fewer than {config.min_training_events_to_publish} training events are marked suppressed and expose zeroed public fields; Reka facts expose null.",
        "feature_interpretation": "Feature drivers indicate the direction of model association for an aggregate row and must never be described as causal.",
        "human_review_required": True,
    }
    validate_contract("model-card", card)
    return card


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise OptionalDependencyError(
            "Prediction export requires pyarrow; install configs/model/requirements.txt"
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")


def _export_tenant(
    *,
    config: ModelConfig,
    config_path: Path,
    manifest: dict[str, Any],
    feature_path: Path,
    rows: list[dict[str, Any]],
    output_root: Path,
) -> dict[str, str]:
    tenant_id = manifest["tenant_id"]
    data_version = manifest["dataset_version"]
    split = chronological_split(rows, config)
    models, _, validation_metrics, report_metrics = _train_and_compare(split, config)
    selected_name, observed_gain, reason = _select_model(validation_metrics, config)
    selected_model = models[selected_name]
    generated_at = utc_now()
    repository_version = code_version(REPOSITORY_ROOT)
    config_checksum = sha256_file(config_path)
    input_checksum = sha256_file(feature_path)
    model_version = f"{selected_name}-{stable_hash(tenant_id, data_version, config_checksum, input_checksum, repository_version, length=16)}"

    report, test_expected, test_risk = _evaluation_report(
        tenant_id=tenant_id,
        model_version=model_version,
        data_version=data_version,
        generated_at=generated_at,
        selected_name=selected_name,
        observed_gain=observed_gain,
        reason=reason,
        report_metrics=report_metrics,
        selected_model=selected_model,
        baseline_model=models["historical_rate"],
        split=split,
        config=config,
    )
    validation_expected = selected_model.predict(split.validation)
    residual_interval = ResidualInterval().fit(
        target_vector(split.validation, config.target), validation_expected
    )
    lower, upper = residual_interval.predict(test_expected)
    predictions = _prediction_rows(
        rows=split.test,
        expected=test_expected,
        risks=test_risk,
        lower=lower,
        upper=upper,
        estimator=selected_model,
        training_rows=split.train,
        tenant_id=tenant_id,
        model_version=model_version,
        data_version=data_version,
        data_as_of=manifest["data_as_of"],
        config=config,
    )
    model_card = _model_card(report, split, config)
    facts = build_fact_bundle(
        tenant_id=tenant_id,
        model_version=model_version,
        data_version=data_version,
        data_as_of=manifest["data_as_of"],
        generated_at=generated_at,
        window_start=format_utc(split.test_start),
        window_end=format_utc(split.test_end + timedelta(hours=config.window_hours)),
        evaluation=report,
        predictions=predictions,
    )
    validate_contract("reka-fact-bundle", facts)

    destination = output_root / f"tenant={tenant_id}" / "models" / model_version
    destination.mkdir(parents=True, exist_ok=True)
    parameters = selected_model.to_bundle()
    serializer = "json_parameters"
    if selected_name == "lightgbm_poisson":
        payload_path = destination / "model.txt"
        payload_path.write_text(parameters["lightgbm_model"], encoding="utf-8")
        serializer = "lightgbm_text"
    else:
        payload_path = destination / "parameters.json"
        write_json(
            payload_path,
            {
                "estimator": parameters,
                "probability_calibration": ProbabilityCalibrator(config.calibration_bins)
                .fit(target_vector(split.validation, config.target), validation_expected)
                .to_dict(),
                "uncertainty": residual_interval.to_dict(),
            },
        )
    requirements_path = REPOSITORY_ROOT / "configs" / "model" / "requirements.txt"
    model_bundle = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "model_version": model_version,
        "data_version": data_version,
        "feature_schema_version": "2.0.0",
        "estimator": selected_name,
        "target": "next_window_count",
        "features": list(config.features),
        "trained_at": generated_at,
        "window_minutes": config.window_hours * 60,
        "serializer": serializer,
        "payload": {
            "path": display_path(payload_path, REPOSITORY_ROOT),
            "sha256": sha256_file(payload_path),
            "size_bytes": payload_path.stat().st_size,
        },
        "runtime": {
            "python_version": platform.python_version(),
            "dependency_lock_sha256": sha256_file(requirements_path),
        },
    }
    validate_contract("model-bundle", model_bundle)

    paths = {
        "model_bundle": destination / "bundle.json",
        "predictions": destination / "predictions.parquet",
        "evaluation_report": destination / "evaluation.json",
        "model_card": destination / "model-card.json",
        "reka_fact_bundle": destination / "reka-facts.json",
    }
    write_json(paths["model_bundle"], model_bundle)
    _write_parquet(paths["predictions"], predictions)
    write_json(paths["evaluation_report"], report)
    write_json(paths["model_card"], model_card)
    write_json(paths["reka_fact_bundle"], facts)

    run_manifest = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "run_id": stable_uuid(tenant_id, model_version, "run"),
        "model_version": model_version,
        "data_version": data_version,
        "target": "next_window_count",
        "generated_at": generated_at,
        "random_seed": config.random_seed,
        "code_version": repository_version,
        "config": {
            "path": display_path(config_path, REPOSITORY_ROOT),
            "sha256": config_checksum,
        },
        "input": {
            "path": display_path(feature_path, REPOSITORY_ROOT),
            "sha256": input_checksum,
        },
        "split": {
            "strategy": "rolling_origin_with_chronological_holdout",
            "train_start": format_utc(split.train_start),
            "train_end": format_utc(split.train_end),
            "validation_start": format_utc(split.validation_start),
            "validation_end": format_utc(split.validation_end),
            "test_start": format_utc(split.test_start),
            "test_end": format_utc(split.test_end),
            "train_rows": len(split.train),
            "validation_rows": len(split.validation),
            "test_rows": len(split.test),
        },
        "candidates": list(validation_metrics),
        "selected_model": selected_name,
        "selection_rule": f"Select the simplest model unless validation {config.primary_metric} improves by at least {config.selection_min_relative_gain:.2%}; evaluate test only after selection.",
        "artifacts": [
            {
                "kind": kind,
                "path": display_path(path, REPOSITORY_ROOT),
                "sha256": sha256_file(path),
            }
            for kind, path in paths.items()
        ],
        "generation_command": f"uv run --python 3.12 --with-requirements configs/model/requirements.txt python -m src.models.cli evaluate --config {display_path(config_path, REPOSITORY_ROOT)} --feature-manifest <tenant-manifest> --output-root {display_path(output_root, REPOSITORY_ROOT)}",
    }
    validate_contract("model-run-manifest", run_manifest)
    manifest_output = destination / "run-manifest.json"
    write_json(manifest_output, run_manifest)
    return {
        "tenant_id": tenant_id,
        "model_version": model_version,
        "selected_model": selected_name,
        "run_manifest": str(manifest_output),
    }


def run_evaluation(
    *,
    config_path: str | Path,
    feature_manifest_paths: list[str | Path],
    output_root: str | Path,
) -> list[dict[str, str]]:
    """Run the complete evaluation independently for every supplied tenant."""
    config_file = Path(config_path).resolve()
    config = ModelConfig.from_path(config_file)
    destination = Path(output_root).resolve()
    results = []
    seen_tenants: set[str] = set()
    for path in feature_manifest_paths:
        _, manifest, feature_path = _read_manifest(path)
        tenant_id = manifest["tenant_id"]
        if tenant_id in seen_tenants:
            raise DataContractError(f"Duplicate manifest for tenant {tenant_id}")
        seen_tenants.add(tenant_id)
        rows = validate_rows(load_rows(feature_path), config)
        row_tenants = {row["tenant_id"] for row in rows}
        if row_tenants != {tenant_id}:
            raise DataContractError(
                f"Feature artifact for {tenant_id} contains tenant set {sorted(row_tenants)}"
            )
        results.append(
            _export_tenant(
                config=config,
                config_path=config_file,
                manifest=manifest,
                feature_path=feature_path,
                rows=rows,
                output_root=destination,
            )
        )
    if not results:
        raise DataContractError("At least one tenant feature manifest is required")
    return results
