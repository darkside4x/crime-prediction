"""Checksum-verified approved-model registry and operational estimator adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
from typing import Any

import numpy as np

from .bundle import load_estimator
from .calibration import IsotonicProbabilityCalibrator
from .config import ModelConfig
from .contracts import REPOSITORY_ROOT, validate_contract
from .errors import DataContractError
from .provenance import sha256_file, stable_uuid


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataContractError(f"Model artifact could not be read: {path.name}") from error
    if not isinstance(payload, dict):
        raise DataContractError(f"Model artifact must be an object: {path.name}")
    return payload


def _inside(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise DataContractError("Model artifact escaped the configured registry root")
    return resolved


class ApprovedOperationalEstimator:
    def __init__(
        self,
        *,
        bundle: dict[str, Any],
        estimator: Any,
        calibrator: IsotonicProbabilityCalibrator,
        calibration_version: str,
        uncertainty: dict[str, Any],
    ) -> None:
        self.tenant_id = bundle["tenant_id"]
        self.model_version = bundle["model_version"]
        self.data_version = bundle["data_version"]
        self.window_minutes = int(bundle["window_minutes"])
        self.estimator = estimator
        self.calibrator = calibrator
        self.calibration_version = calibration_version
        self.uncertainty = uncertainty

    def predict(self, rows: list[dict[str, Any]]) -> np.ndarray:
        return self.estimator.predict(rows)

    def drivers(self, row: dict[str, Any], limit: int = 5) -> list[dict[str, str]]:
        return self.estimator.drivers(row, limit=limit)

    def calibrate_probability(self, raw_probability: float) -> float:
        return float(self.calibrator.predict(raw_probability)[()])

    def count_interval(self, row: dict[str, Any], expected: float) -> tuple[float, float, str]:
        coverage = float(row["coverage_ratio"])
        inflation = 1.0 + (1.0 - coverage) * float(
            self.uncertainty["coverage_inflation_per_missing_ratio"]
        )
        lower = max(0.0, expected + float(self.uncertainty["residual_lower"]) * inflation)
        upper = max(expected, expected + float(self.uncertainty["residual_upper"]) * inflation)
        return min(lower, expected), upper, str(self.uncertainty["method"])


class FilesystemApprovedModelRegistry:
    """Atomic approval state over immutable, checksum-verified model artifacts."""

    development_only = False

    def __init__(
        self,
        root: str | Path,
        *,
        config: ModelConfig | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config or ModelConfig()
        self.config.validate()
        self.state_path = _inside(
            self.root, Path(state_path) if state_path is not None else self.root / "registry-state.json"
        )
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str], ApprovedOperationalEstimator] = {}

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": "1.0.0", "tenants": {}}
        state = _read_json(self.state_path)
        if state.get("schema_version") != "1.0.0" or not isinstance(state.get("tenants"), dict):
            raise DataContractError("Model registry state is invalid")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.state_path)

    def _bundle_path(self, tenant_id: str, model_version: str) -> Path:
        if "/" in tenant_id or "/" in model_version or ".." in tenant_id or ".." in model_version:
            raise DataContractError("Model registry identifiers are invalid")
        return _inside(
            self.root,
            self.root / f"tenant={tenant_id}" / "models" / model_version / "bundle.json",
        )

    def _run_manifest(self, bundle_path: Path) -> tuple[dict[str, Any], Path]:
        path = bundle_path.parent / "run-manifest.json"
        manifest = _read_json(path)
        validate_contract("model-run-manifest", manifest)
        return manifest, path

    def _verified_artifact(
        self,
        manifest: dict[str, Any],
        bundle_path: Path,
        *,
        kind: str,
        filename: str,
    ) -> Path:
        entry = next((item for item in manifest["artifacts"] if item["kind"] == kind), None)
        if entry is None:
            raise DataContractError(f"Run manifest is missing the {kind} artifact")
        expected = _inside(self.root, bundle_path.parent / filename)
        declared = Path(entry["path"])
        candidates = (
            [declared]
            if declared.is_absolute()
            else [REPOSITORY_ROOT / declared, bundle_path.parent / declared.name]
        )
        resolved = next((item.resolve() for item in candidates if item.exists()), None)
        if resolved is None or _inside(self.root, resolved) != expected:
            raise DataContractError(f"Run manifest {kind} path does not match the registry artifact")
        if sha256_file(expected) != entry["sha256"]:
            raise DataContractError(f"Run manifest {kind} checksum does not match")
        return expected

    def _load(
        self,
        tenant_id: str,
        model_version: str,
        *,
        expected_manifest_sha256: str | None = None,
    ) -> ApprovedOperationalEstimator:
        cache_key = (tenant_id, model_version)
        bundle_path = self._bundle_path(tenant_id, model_version)
        manifest, manifest_path = self._run_manifest(bundle_path)
        if manifest["tenant_id"] != tenant_id or manifest["model_version"] != model_version:
            raise DataContractError("Run manifest tenant or version does not match registry selection")
        if expected_manifest_sha256 is not None and sha256_file(manifest_path) != expected_manifest_sha256:
            raise DataContractError("Approved run manifest checksum changed after promotion")
        self._verified_artifact(manifest, bundle_path, kind="model_bundle", filename="bundle.json")
        calibration_path = self._verified_artifact(
            manifest, bundle_path, kind="calibration", filename="calibration.json"
        )
        uncertainty_path = self._verified_artifact(
            manifest, bundle_path, kind="uncertainty", filename="uncertainty.json"
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        bundle = _read_json(bundle_path)
        validate_contract("model-bundle", bundle)
        if bundle["tenant_id"] != tenant_id or bundle["model_version"] != model_version:
            raise DataContractError("Model bundle tenant or version does not match registry selection")
        declared_payload = Path(bundle["payload"]["path"])
        candidates = (
            [declared_payload]
            if declared_payload.is_absolute()
            else [REPOSITORY_ROOT / declared_payload, bundle_path.parent / declared_payload.name]
        )
        payload_path = next((path.resolve() for path in candidates if path.exists()), None)
        if payload_path is None:
            raise DataContractError("Approved model payload does not exist")
        _inside(self.root, payload_path)
        if payload_path.stat().st_size != bundle["payload"]["size_bytes"]:
            raise DataContractError("Approved model payload size does not match bundle metadata")
        estimator = load_estimator(bundle, payload_path, self.config)
        calibration_payload = _read_json(calibration_path)
        uncertainty = _read_json(uncertainty_path)
        if calibration_payload.get("model_version") != model_version:
            raise DataContractError("Calibration artifact model version does not match")
        if uncertainty.get("model_version") != model_version:
            raise DataContractError("Uncertainty artifact model version does not match")
        required_uncertainty = {
            "uncertainty_version",
            "method",
            "interval_level",
            "residual_lower",
            "residual_upper",
            "coverage_inflation_per_missing_ratio",
            "components",
            "estimated_from",
            "model_version",
        }
        if set(uncertainty) != required_uncertainty:
            raise DataContractError("Uncertainty artifact fields are invalid")
        if uncertainty["method"] != "rolling_origin_model_data_temporal_v1":
            raise DataContractError("Uncertainty artifact method is not approved")
        numeric = (
            uncertainty["interval_level"],
            uncertainty["residual_lower"],
            uncertainty["residual_upper"],
            uncertainty["coverage_inflation_per_missing_ratio"],
        )
        if any(not math.isfinite(float(value)) for value in numeric):
            raise DataContractError("Uncertainty artifact contains a non-finite value")
        if not 0 < float(uncertainty["interval_level"]) < 1:
            raise DataContractError("Uncertainty interval level is invalid")
        if float(uncertainty["residual_lower"]) > 0 or float(uncertainty["residual_upper"]) < 0:
            raise DataContractError("Uncertainty residual bounds are invalid")
        if float(uncertainty["coverage_inflation_per_missing_ratio"]) < 0:
            raise DataContractError("Uncertainty coverage inflation is invalid")
        if set(uncertainty["components"]) != {
            "model_refit_variation",
            "temporal_residual_variation",
            "data_coverage_availability",
        }:
            raise DataContractError("Uncertainty artifact does not include all required components")
        calibrator = IsotonicProbabilityCalibrator.from_dict(calibration_payload)
        approved = ApprovedOperationalEstimator(
            bundle=bundle,
            estimator=estimator,
            calibrator=calibrator,
            calibration_version=str(calibration_payload["calibration_version"]),
            uncertainty=uncertainty,
        )
        self._cache[cache_key] = approved
        return approved

    def approved_for(self, tenant_id: str) -> ApprovedOperationalEstimator | None:
        with self._lock:
            record = self._state()["tenants"].get(tenant_id)
            if not record or not record.get("active_model_version"):
                return None
            active = str(record["active_model_version"])
            approval = next(
                (
                    item
                    for item in reversed(record.get("history", []))
                    if item.get("model_version") == active
                ),
                None,
            )
            if approval is None or not approval.get("run_manifest_sha256"):
                raise DataContractError("Approved model is missing its frozen run manifest checksum")
            return self._load(
                tenant_id,
                active,
                expected_manifest_sha256=str(approval["run_manifest_sha256"]),
            )

    def promote(
        self,
        tenant_id: str,
        model_version: str,
        *,
        approved_by: str,
        reason: str,
    ) -> dict[str, Any]:
        if not 1 <= len(reason) <= 500:
            raise DataContractError("Model approval reason must contain 1 to 500 characters")
        with self._lock:
            model = self._load(tenant_id, model_version)
            manifest_sha256 = sha256_file(self._bundle_path(tenant_id, model_version).parent / "run-manifest.json")
            state = self._state()
            previous = state["tenants"].get(tenant_id, {}).get("active_model_version")
            record = {
                "approval_id": stable_uuid(tenant_id, model_version, approved_by, _utc_now()),
                "model_version": model.model_version,
                "approved_by": approved_by,
                "approved_at": _utc_now(),
                "reason": reason,
                "previous_model_version": previous,
                "run_manifest_sha256": manifest_sha256,
            }
            tenant_state = state["tenants"].setdefault(
                tenant_id, {"active_model_version": None, "history": []}
            )
            tenant_state["active_model_version"] = model_version
            tenant_state["history"].append(record)
            self._write_state(state)
            return dict(record)

    def rollback(self, tenant_id: str, *, approved_by: str, reason: str) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            tenant_state = state["tenants"].get(tenant_id)
            if not tenant_state or len(tenant_state.get("history", [])) < 2:
                raise DataContractError("No previously approved model is available for rollback")
            previous = tenant_state["history"][-1].get("previous_model_version")
            if not previous:
                raise DataContractError("No previously approved model is available for rollback")
        return self.promote(tenant_id, str(previous), approved_by=approved_by, reason=reason)

    def status(self, tenant_id: str) -> dict[str, Any]:
        with self._lock:
            tenant_state = self._state()["tenants"].get(
                tenant_id, {"active_model_version": None, "history": []}
            )
            return json.loads(json.dumps(tenant_state))

    def model_card_for(self, tenant_id: str) -> dict[str, Any] | None:
        status = self.status(tenant_id)
        model_version = status.get("active_model_version")
        if not model_version:
            return None
        self.approved_for(tenant_id)
        bundle_path = self._bundle_path(tenant_id, str(model_version))
        manifest, _ = self._run_manifest(bundle_path)
        card_path = self._verified_artifact(
            manifest, bundle_path, kind="model_card", filename="model-card.json"
        )
        card = _read_json(card_path)
        validate_contract("model-card", card)
        if card["tenant_id"] != tenant_id or card["model_version"] != model_version:
            raise DataContractError("Approved model card tenant or version does not match")
        return card
