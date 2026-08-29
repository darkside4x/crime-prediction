"""Synthetic tenant feature-table builders for offline tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src.models.provenance import sha256_file


TENANTS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
)
CELLS = ("8861892581fffff", "8861892583fffff")
CATEGORIES = ("property", "public-order")


def synthetic_rows(tenant_id: str, intervals: int = 36) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    histories: dict[tuple[str, str], list[int]] = {
        (cell, category): [] for cell in CELLS for category in CATEGORIES
    }
    output = []
    tenant_shift = 1 if tenant_id.endswith("2") else 0
    for interval in range(intervals):
        timestamp = start + timedelta(hours=6 * interval)
        for cell_index, cell in enumerate(CELLS):
            for category_index, category in enumerate(CATEGORIES):
                history = histories[(cell, category)]
                count = int(
                    (interval + cell_index + category_index + tenant_shift) % 5 == 0
                    or (cell_index == 1 and interval % 9 == 0)
                )

                def lag(distance: int) -> int:
                    return history[-distance] if len(history) >= distance else 0

                rolling_7 = history[-7:]
                rolling_14 = history[-14:]
                neighbor_history = histories[(CELLS[1 - cell_index], category)]
                output.append(
                    {
                        "schema_version": "2.0.0",
                        "tenant_id": tenant_id,
                        "cell_id": cell,
                        "interval_start": timestamp.isoformat().replace("+00:00", "Z"),
                        "category": category,
                        "event_count": count,
                        "lag_1": lag(1),
                        "lag_2": lag(2),
                        "lag_7": lag(7),
                        "lag_14": lag(14),
                        "rolling_7_mean": sum(rolling_7) / len(rolling_7) if rolling_7 else 0.0,
                        "rolling_14_mean": sum(rolling_14) / len(rolling_14) if rolling_14 else 0.0,
                        "neighbor_lag_1": float(neighbor_history[-1]) if neighbor_history else 0.0,
                        "recent_trend": (sum(rolling_7) / len(rolling_7) if rolling_7 else 0.0)
                        - (sum(rolling_14) / len(rolling_14) if rolling_14 else 0.0),
                        "hour_sin": math.sin(2 * math.pi * timestamp.hour / 24),
                        "hour_cos": math.cos(2 * math.pi * timestamp.hour / 24),
                        "day_of_week_sin": math.sin(2 * math.pi * timestamp.weekday() / 7),
                        "day_of_week_cos": math.cos(2 * math.pi * timestamp.weekday() / 7),
                        "coverage_ratio": 1.0 if interval % 8 else 0.75,
                        "data_as_of": (timestamp - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                    }
                )
                history.append(count)
    return output


def write_feature_manifest(root: Path, tenant_id: str, rows: list[dict] | None = None) -> Path:
    rows = rows or synthetic_rows(tenant_id)
    tenant_root = root / f"tenant={tenant_id}" / "features"
    tenant_root.mkdir(parents=True, exist_ok=True)
    parquet_path = tenant_root / "features.parquet"
    pq.write_table(pa.Table.from_pylist(rows), parquet_path, compression="zstd")
    manifest = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "dataset_version": f"synthetic-{tenant_id[-1]}-v1",
        "feature_schema_version": "2.0.0",
        "artifact": {
            "path": str(parquet_path),
            "format": "parquet",
            "sha256": sha256_file(parquet_path),
        },
        "source_versions": [
            {
                "source_id": f"10000000-0000-4000-8000-00000000000{tenant_id[-1]}",
                "source_version": "synthetic-v1",
            }
        ],
        "generated_at": "2026-01-10T00:00:00Z",
        "data_as_of": rows[-1]["data_as_of"],
        "interval_minutes": 360,
        "row_count": len(rows),
        "interval_start_min": rows[0]["interval_start"],
        "interval_start_max": rows[-1]["interval_start"],
        "categories": list(CATEGORIES),
        "cell_count": len(CELLS),
        "generation_command": "synthetic test fixture",
        "code_version": "test",
        "parameters": {"h3_resolution": 8, "window_hours": 6},
    }
    manifest_path = tenant_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def write_test_config(root: Path, *, enable_lightgbm: bool = False) -> Path:
    source = Path("configs/model/default.json")
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload.update(
        {
            "train_fraction": 0.5,
            "validation_fraction": 0.25,
            "min_intervals_per_split": 4,
            "enable_lightgbm": enable_lightgbm,
            "rolling_origins": 2,
            "bootstrap_samples": 10,
            "calibration_bins": 4,
        }
    )
    path = root / "model-config.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
