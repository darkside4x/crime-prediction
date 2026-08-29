from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h3
import polars as pl

from src.data.source import SourceDefinition
from src.data.store import IngestionStore
from src.features.builder import FeatureBuildConfig, FeatureBuilder


TENANT_A = "00000000-0000-4000-8000-000000000001"
TENANT_B = "00000000-0000-4000-8000-000000000002"
SOURCE_A = "00000000-0000-4000-8000-000000000101"
SOURCE_B = "00000000-0000-4000-8000-000000000202"
CELL_A = h3.latlng_to_cell(12.9716, 77.5946, 8)
CELL_B = h3.latlng_to_cell(12.98, 77.6, 8)
UTC = timezone.utc


def register_source(store: IngestionStore, tenant_id: str, source_id: str) -> None:
    store.register_source(
        SourceDefinition.from_dict(
            {
                "schema_version": "1.0.0",
                "tenant_id": tenant_id,
                "source_id": source_id,
                "name": "Feature fixture",
                "kind": "recorded_replay",
                "status": "active",
                "config": {"format": "jsonl", "location_ref": "fixture.jsonl"},
                "created_at": "2026-08-01T00:00:00Z",
            }
        )
    )


def add_event(
    store: IngestionStore,
    event_id: str,
    occurred_at: str,
    received_at: str,
    *,
    tenant_id: str = TENANT_A,
    source_id: str = SOURCE_A,
    latitude: float = 12.9716,
    longitude: float = 77.5946,
) -> None:
    payload = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "source_id": source_id,
        "external_event_id": event_id,
        "occurred_at": occurred_at,
        "received_at": received_at,
        "category": "property",
        "location": {"latitude": latitude, "longitude": longitude},
        "attributes": {},
    }
    event_hash = hashlib.sha256(str(payload).encode()).hexdigest()
    assert store.insert_event(payload, event_hash)


def config() -> FeatureBuildConfig:
    return FeatureBuildConfig(
        tenant_id=TENANT_A,
        source_ids=(SOURCE_A,),
        start=datetime(2026, 8, 1, 12, tzinfo=UTC),
        end=datetime(2026, 8, 2, 6, tzinfo=UTC),
        interval=timedelta(hours=6),
        h3_resolution=8,
        domain_cells=(CELL_A, CELL_B),
        categories=("property",),
    )


def row_at(rows: list[dict[str, object]], cell: str, timestamp: str) -> dict[str, object]:
    return next(row for row in rows if row["cell_id"] == cell and row["interval_start"] == timestamp)


def test_features_are_time_complete_point_in_time_correct_and_tenant_scoped(tmp_path: Path) -> None:
    store = IngestionStore(tmp_path / "state.sqlite")
    register_source(store, TENANT_A, SOURCE_A)
    register_source(store, TENANT_B, SOURCE_B)
    add_event(store, "a-00", "2026-08-01T00:10:00Z", "2026-08-01T00:11:00Z")
    add_event(store, "a-06", "2026-08-01T06:10:00Z", "2026-08-01T06:11:00Z")
    add_event(store, "a-late", "2026-08-01T00:20:00Z", "2026-08-01T19:00:00Z")
    add_event(
        store,
        "b-06",
        "2026-08-01T06:10:00Z",
        "2026-08-01T06:11:00Z",
        tenant_id=TENANT_B,
        source_id=SOURCE_B,
    )

    builder = FeatureBuilder(store)
    rows = builder.build_rows(config())
    assert len(rows) == 6

    at_12 = row_at(rows, CELL_A, "2026-08-01T12:00:00Z")
    assert at_12["lag_1"] == 1
    assert at_12["lag_2"] == 1
    assert at_12["rolling_7_mean"] == 2 / 7

    cell_b_at_12 = row_at(rows, CELL_B, "2026-08-01T12:00:00Z")
    assert cell_b_at_12["event_count"] == 0
    assert cell_b_at_12["neighbor_lag_1"] == 1.0

    at_18 = row_at(rows, CELL_A, "2026-08-01T18:00:00Z")
    assert at_18["rolling_7_mean"] == 2 / 7

    at_24 = row_at(rows, CELL_A, "2026-08-02T00:00:00Z")
    assert at_24["rolling_7_mean"] == 3 / 7
    assert all(row["tenant_id"] == TENANT_A for row in rows)


def test_future_record_cannot_change_earlier_rows(tmp_path: Path) -> None:
    store = IngestionStore(tmp_path / "state.sqlite")
    register_source(store, TENANT_A, SOURCE_A)
    add_event(store, "past", "2026-08-01T06:10:00Z", "2026-08-01T06:11:00Z")
    builder = FeatureBuilder(store)
    before = builder.build_rows(config())

    add_event(store, "future", "2026-08-02T12:00:00Z", "2026-08-02T12:01:00Z")
    after = builder.build_rows(config())

    assert before == after


def test_parquet_contains_no_raw_coordinates_or_event_identifiers(tmp_path: Path) -> None:
    store = IngestionStore(tmp_path / "state.sqlite")
    register_source(store, TENANT_A, SOURCE_A)
    add_event(store, "private-id", "2026-08-01T06:10:00Z", "2026-08-01T06:11:00Z")
    replay_input = tmp_path / "input.jsonl"
    replay_input.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "features.parquet"
    manifest = tmp_path / "manifest.json"

    FeatureBuilder(store).write_parquet(
        config(),
        output,
        manifest,
        source_versions={SOURCE_A: "synthetic-v1"},
        category_map_version="1.0.0",
        replay_input_path=replay_input,
        generation_command=["crime-data", "replay"],
    )

    columns = set(pl.read_parquet(output).columns)
    assert {"latitude", "longitude", "external_event_id", "source_id"}.isdisjoint(columns)
    assert output.is_file()
    assert manifest.is_file()

    rows = pl.read_parquet(output).to_dicts()
    assert {row["schema_version"] for row in rows} == {"2.0.0"}
    assert all(row["data_as_of"] < row["interval_start"] for row in rows)
