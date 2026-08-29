"""Feature-table loading and contract validation."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import ModelConfig
from .errors import DataContractError, OptionalDependencyError


IDENTITY_FIELDS = ("tenant_id", "cell_id", "interval_start", "category")
FORBIDDEN_FIELDS = {
    "latitude",
    "longitude",
    "location",
    "external_event_id",
    "source_id",
    "address",
}


def parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise DataContractError("interval_start and data_as_of must be ISO-8601 strings")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DataContractError(f"Invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise DataContractError(f"Timestamp must include a UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def _coerce_csv_value(key: str, value: str) -> Any:
    if key in IDENTITY_FIELDS or key in {"schema_version", "data_as_of"}:
        return value
    try:
        return float(value)
    except ValueError:
        return value


def load_rows(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise DataContractError(f"Feature table does not exist: {source}")
    suffix = source.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        rows = []
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DataContractError(f"Invalid JSON on line {line_number}: {exc}") from exc
        return rows
    if suffix == ".csv":
        with source.open(newline="", encoding="utf-8") as handle:
            return [
                {key: _coerce_csv_value(key, value) for key, value in row.items()}
                for row in csv.DictReader(handle)
            ]
    if suffix == ".parquet":
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:
            raise OptionalDependencyError(
                "Parquet input requires pyarrow; install configs/model/requirements.txt"
            ) from exc
        # ParquetFile reads the physical file only. Dataset readers may infer
        # untrusted Hive columns from parent paths such as tenant=<id>.
        return parquet.ParquetFile(source).read().to_pylist()
    raise DataContractError(f"Unsupported feature-table extension: {suffix}")


def validate_rows(rows: Iterable[dict[str, Any]], config: ModelConfig) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    required = set(IDENTITY_FIELDS) | {config.target, "data_as_of"} | set(config.features)
    seen_keys: set[tuple[str, str, datetime, str]] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise DataContractError(f"Row {index} is not an object")
        forbidden = FORBIDDEN_FIELDS.intersection(raw)
        if forbidden:
            raise DataContractError(f"Row {index} contains restricted fields: {sorted(forbidden)}")
        missing = required.difference(raw)
        if missing:
            raise DataContractError(f"Row {index} is missing fields: {sorted(missing)}")
        if raw.get("schema_version") != "2.0.0":
            raise DataContractError(f"Row {index} has an unsupported schema_version")
        interval_start = parse_utc(raw["interval_start"])
        data_as_of = parse_utc(raw["data_as_of"])
        if data_as_of >= interval_start:
            raise DataContractError(
                f"Row {index} has data_as_of at or after interval_start; features must be strictly prior"
            )
        tenant_id = str(raw["tenant_id"])
        cell_id = str(raw["cell_id"])
        category = str(raw["category"])
        if not tenant_id or not cell_id or not category:
            raise DataContractError(f"Row {index} has an empty prediction-key component")
        key = (tenant_id, cell_id, interval_start, category)
        if key in seen_keys:
            raise DataContractError(f"Duplicate prediction key at row {index}: {key}")
        seen_keys.add(key)
        target = float(raw[config.target])
        if not math.isfinite(target) or target < 0 or not target.is_integer():
            raise DataContractError(f"Row {index} target must be a non-negative integer")
        normalized = dict(raw)
        normalized["interval_start"] = interval_start
        normalized["data_as_of"] = data_as_of
        normalized[config.target] = target
        for feature in config.features:
            value = float(raw[feature])
            if not math.isfinite(value):
                raise DataContractError(f"Row {index} feature {feature!r} is not finite")
            normalized[feature] = value
        validated.append(normalized)
    if not validated:
        raise DataContractError("Feature table contains no rows")
    return sorted(
        validated,
        key=lambda row: (row["tenant_id"], row["interval_start"], row["cell_id"], row["category"]),
    )


def design_matrix(rows: list[dict[str, Any]], features: tuple[str, ...]) -> np.ndarray:
    return np.asarray([[row[name] for name in features] for row in rows], dtype=float)


def target_vector(rows: list[dict[str, Any]], target: str) -> np.ndarray:
    return np.asarray([row[target] for row in rows], dtype=float)
