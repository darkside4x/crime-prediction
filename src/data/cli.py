"""Command-line entry point for recorded ingestion and feature generation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

from src.data.adapters import RecordedReplayAdapter
from src.data.category_map import CategoryMap
from src.data.service import IngestionService
from src.data.source import SourceDefinition
from src.data.store import IngestionStore
from src.features.builder import (
    FeatureBuildConfig,
    FeatureBuilder,
    load_domain_cells,
    parse_utc,
    sha256_file,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crime-data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="Replay JSONL incidents and build aggregate features")
    replay.add_argument("--source-definition", type=Path, required=True)
    replay.add_argument("--input", type=Path)
    replay.add_argument("--state-db", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--manifest", type=Path, required=True)
    replay.add_argument(
        "--category-map",
        type=Path,
        default=REPOSITORY_ROOT / "configs" / "data" / "category-map.json",
    )
    replay.add_argument("--domain-cells", type=Path, required=True)
    replay.add_argument("--start", required=True, help="Aligned ISO 8601 timestamp")
    replay.add_argument("--end", required=True, help="Aligned exclusive ISO 8601 timestamp")
    replay.add_argument("--interval-hours", type=float, default=6.0)
    replay.add_argument("--h3-resolution", type=int, default=8)
    replay.add_argument("--coverage-ratio", type=float, default=1.0)
    replay.add_argument(
        "--tenant-id",
        help="Authenticated tenant context for the local demo; defaults to the source tenant",
    )
    return parser


async def run_replay(args: argparse.Namespace) -> int:
    source_path = args.source_definition.resolve()
    source = SourceDefinition.from_file(source_path)
    authenticated_tenant_id = args.tenant_id or source.tenant_id

    if args.input:
        input_path = args.input.resolve()
    else:
        location_ref = source.config.get("location_ref")
        if not location_ref:
            raise ValueError("Recorded source is missing config.location_ref")
        input_path = (source_path.parent / location_ref).resolve()
        try:
            input_path.relative_to(source_path.parent)
        except ValueError as exc:
            raise ValueError(
                "Recorded source config.location_ref must stay within the source-definition directory; "
                "use --input for an explicit external path"
            ) from exc

    category_map = CategoryMap.from_file(args.category_map.resolve())
    store = IngestionStore(args.state_db.resolve())
    adapter = RecordedReplayAdapter(source, store, input_path)
    ingestion = IngestionService(store, category_map)
    run = await ingestion.ingest_replay(
        adapter,
        source,
        authenticated_tenant_id=authenticated_tenant_id,
    )

    config = FeatureBuildConfig(
        tenant_id=authenticated_tenant_id,
        source_ids=(source.source_id,),
        start=parse_utc(args.start),
        end=parse_utc(args.end),
        interval=timedelta(hours=args.interval_hours),
        h3_resolution=args.h3_resolution,
        domain_cells=load_domain_cells(args.domain_cells.resolve()),
        categories=category_map.canonical_categories,
        coverage_ratio=args.coverage_ratio,
    )
    builder = FeatureBuilder(store)
    manifest = builder.write_parquet(
        config,
        args.output.resolve(),
        args.manifest.resolve(),
        source_versions={source.source_id: f"sha256:{sha256_file(source_path)}"},
        category_map_version=f"sha256:{sha256_file(args.category_map.resolve())}",
        replay_input_path=input_path,
        generation_command=["crime-data", *sys.argv[1:]],
    )
    print(
        json.dumps(
            {
                "ingestion_run": run,
                "feature_output": str(args.output.resolve()),
                "manifest": str(args.manifest.resolve()),
                "row_count": manifest["row_count"],
                "feature_parquet_sha256": manifest["artifact"]["sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "replay":
        return asyncio.run(run_replay(args))
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
