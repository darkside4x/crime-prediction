"""Build and evaluate the bounded DataSF 2024-H1 benchmark.

Raw identifiers and approximate coordinates are read only at this ingestion
boundary and persisted only in the caller-supplied restricted SQLite file.
Generated features and model artifacts contain H3 cells and aggregate counts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import h3

from src.data.category_map import CategoryMap
from src.data.errors import ContractValidationError, IngestionError
from src.data.service import IngestionService
from src.data.source import SourceDefinition
from src.data.store import IngestionStore, utc_now
from src.features.builder import FeatureBuildConfig, FeatureBuilder, sha256_file
from src.models.pipeline import run_evaluation


TENANT_ID = "00000000-0000-4000-8000-0000000000sf".replace("sf", "51")
SOURCE_ID = "00000000-0000-4000-8000-000000000052"
API_RESOURCE = "https://data.sfgov.org/resource/wg3w-h783.json"
API_WINDOW_START = "2024-01-01T00:00:00"
API_WINDOW_END = "2024-07-01T00:00:00"
API_BOUNDS = (37.75, 37.81, -122.46, -122.39)
FEATURE_START = datetime(2024, 1, 2, tzinfo=timezone.utc)
FEATURE_END = datetime(2024, 7, 1, tzinfo=timezone.utc)
PACIFIC = ZoneInfo("America/Los_Angeles")


PROPERTY = {
    "Arson", "Burglary", "Embezzlement", "Forgery And Counterfeiting",
    "Fraud", "Larceny Theft", "Lost Property", "Motor Vehicle Theft",
    "Robbery", "Stolen Property",
}
VIOLENCE = {"Assault", "Homicide", "Human Trafficking (A), Commercial Sex Acts", "Rape"}
PUBLIC_ORDER = {
    "Disorderly Conduct", "Drug Offense", "Drug Violation", "Liquor Laws",
    "Malicious Mischief", "Prostitution", "Vandalism", "Weapons Carrying Etc",
    "Weapons Offense",
}
TRAFFIC = {"Traffic Collision", "Traffic Violation Arrest", "Vehicle Impounded"}


def _category(value: str) -> str:
    if value in PROPERTY:
        return "property"
    if value in VIOLENCE:
        return "violence"
    if value in PUBLIC_ORDER:
        return "public_order"
    if value in TRAFFIC:
        return "traffic_safety"
    return "other"


def _utc(value: str) -> str:
    local = datetime.fromisoformat(value).replace(tzinfo=PACIFIC)
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source(raw_path: Path) -> SourceDefinition:
    created = "2024-07-02T00:00:00Z"
    return SourceDefinition.from_dict(
        {
            "schema_version": "1.0.0",
            "tenant_id": TENANT_ID,
            "source_id": SOURCE_ID,
            "name": "DataSF police incident reports 2024-H1 benchmark",
            "kind": "recorded_replay",
            "status": "active",
            "config": {
                "format": "json",
                "location_ref": raw_path.name,
                "loop": False,
                "timezone": "America/Los_Angeles",
            },
            "created_at": created,
        }
    )


def ingest(raw_path: Path, state_db: Path) -> tuple[IngestionStore, int, int]:
    records = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("DataSF response must be an array")
    store = IngestionStore(state_db)
    source = _source(raw_path)
    store.register_source(source)
    normalizer = IngestionService(
        store,
        CategoryMap(
            schema_version="1.0.0",
            canonical_categories=("property", "violence", "public_order", "traffic_safety", "other"),
            aliases={},
            unknown_policy="other",
        ),
    )
    domain = set(h3.grid_disk(h3.latlng_to_cell(37.78, -122.42, 8), 2))
    accepted: list[tuple[object, ...]] = []
    rejected = 0
    for record in records:
        try:
            latitude = float(record["latitude"])
            longitude = float(record["longitude"])
            if h3.latlng_to_cell(latitude, longitude, 8) not in domain:
                continue
            event = normalizer.normalize_event(
                {
                    "schema_version": "1.0.0",
                    "tenant_id": TENANT_ID,
                    "source_id": SOURCE_ID,
                    "external_event_id": str(record["row_id"]),
                    "occurred_at": _utc(record["incident_datetime"]),
                    "received_at": _utc(record["report_datetime"]),
                    "category": _category(str(record.get("incident_category", ""))),
                    "location": {
                        "latitude": latitude,
                        "longitude": longitude,
                        "accuracy_meters": 200,
                    },
                    "attributes": {"source_quality": "official_approximate_location"},
                },
                authenticated_tenant_id=TENANT_ID,
                source=source,
            )
        except (ContractValidationError, IngestionError, KeyError, TypeError, ValueError):
            # Invalid rows are counted, but raw records are never copied to logs/artifacts.
            rejected += 1
            continue
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        location = event["location"]
        accepted.append(
            (
                event["tenant_id"], event["source_id"], event["external_event_id"],
                event["occurred_at"], event["received_at"], event["category"],
                location["latitude"], location["longitude"], location["accuracy_meters"],
                "null", json.dumps(event["attributes"], sort_keys=True),
                hashlib.sha256(encoded).hexdigest(), utc_now(),
            )
        )
    with store.connect() as connection:
        connection.executemany(
            """INSERT OR IGNORE INTO accepted_events
               (tenant_id,source_id,external_event_id,occurred_at,received_at,category,
                latitude,longitude,accuracy_meters,source_sequence_json,attributes_json,
                event_hash,ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            accepted,
        )
    return store, len(accepted), rejected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-json", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, default=Path("configs/model/datasf-benchmark.json"))
    args = parser.parse_args()
    raw_path = args.raw_json.resolve()
    work_root = args.work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    store, accepted, rejected = ingest(raw_path, args.state_db.resolve())
    domain = tuple(sorted(h3.grid_disk(h3.latlng_to_cell(37.78, -122.42, 8), 2)))
    feature_path = work_root / "features.parquet"
    feature_manifest = work_root / "feature-manifest.json"
    builder = FeatureBuilder(store)
    manifest = builder.write_parquet(
        FeatureBuildConfig(
            tenant_id=TENANT_ID,
            source_ids=(SOURCE_ID,),
            start=FEATURE_START,
            end=FEATURE_END,
            interval=timedelta(hours=6),
            h3_resolution=8,
            domain_cells=domain,
            categories=("property", "violence", "public_order", "traffic_safety", "other"),
            # This historical public dataset has no camera uptime signal. This is
            # an explicit benchmark-only source-availability assumption; product
            # inference reads measured camera/source telemetry instead.
            coverage_ratio=1.0,
        ),
        feature_path,
        feature_manifest,
        source_versions={SOURCE_ID: f"datasf-wg3w-h783-sha256:{sha256_file(raw_path)}"},
        category_map_version="datasf-category-map-1.0.0",
        replay_input_path=raw_path,
        generation_command=["python", "scripts/run_datasf_benchmark.py"],
    )
    result = run_evaluation(
        config_path=args.model_config,
        feature_manifest_paths=[feature_manifest],
        output_root=work_root / "artifacts",
    )
    print(json.dumps({"accepted": accepted, "rejected": rejected, "feature_manifest": manifest, "model": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
