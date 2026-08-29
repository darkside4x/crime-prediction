"""Time-complete H3 feature tables with point-in-time-correct predictors."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import h3
import polars as pl

from src.data.contracts import validate_contract
from src.data.store import IngestionStore, utc_now


CountKey = tuple[str, str, datetime]


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def floor_interval(value: datetime, interval: timedelta) -> datetime:
    seconds = int(interval.total_seconds())
    if seconds <= 0:
        raise ValueError("Interval must be positive")
    epoch_seconds = int(value.astimezone(timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch_seconds - epoch_seconds % seconds, tz=timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path, repository_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


@dataclass(frozen=True)
class FeatureBuildConfig:
    tenant_id: str
    source_ids: tuple[str, ...]
    start: datetime
    end: datetime
    interval: timedelta
    h3_resolution: int
    domain_cells: tuple[str, ...]
    categories: tuple[str, ...]
    coverage_ratio: float = 1.0

    def validate(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Feature range timestamps must include timezones")
        if self.end <= self.start:
            raise ValueError("Feature range end must be after start")
        if self.interval.total_seconds() <= 0:
            raise ValueError("Feature interval must be positive")
        if self.interval.total_seconds() % 60:
            raise ValueError("Feature interval must be a whole number of minutes")
        if not self.domain_cells:
            raise ValueError("A fixed, externally defined H3 domain is required")
        if not self.source_ids:
            raise ValueError("At least one tenant-owned source is required")
        if not self.categories:
            raise ValueError("At least one canonical category is required")
        if not 0 <= self.coverage_ratio <= 1:
            raise ValueError("coverage_ratio must be between zero and one")
        for cell in self.domain_cells:
            if not h3.is_valid_cell(cell):
                raise ValueError(f"Invalid H3 cell: {cell}")
            if h3.get_resolution(cell) != self.h3_resolution:
                raise ValueError(f"H3 cell {cell} is not resolution {self.h3_resolution}")


@dataclass(frozen=True)
class PreparedEvent:
    occurred_at: datetime
    received_at: datetime
    category: str
    cell_id: str


class FeatureBuilder:
    def __init__(self, store: IngestionStore) -> None:
        self.store = store

    def _prepare_events(self, config: FeatureBuildConfig) -> list[PreparedEvent]:
        domain = set(config.domain_cells)
        categories = set(config.categories)
        events: list[PreparedEvent] = []
        aggregated = self.store.list_aggregated_events(
            config.tenant_id,
            config.h3_resolution,
            config.source_ids,
        )
        for row in aggregated:
            cell_id = row["cell_id"]
            if cell_id not in domain or row["category"] not in categories:
                continue
            events.append(
                PreparedEvent(
                    occurred_at=parse_utc(row["occurred_at"]),
                    received_at=parse_utc(row["received_at"]),
                    category=row["category"],
                    cell_id=cell_id,
                )
            )
        return events

    def build_rows(self, config: FeatureBuildConfig) -> list[dict[str, Any]]:
        """Build features.

        `event_count` is the supervised label for the interval beginning at
        `interval_start`. Every other count uses only events with both an
        occurrence bucket before `interval_start` and `received_at` strictly
        before it. The cell domain is supplied externally so future incident
        locations cannot cause a cell to appear in an earlier feature table.
        """
        config.validate()
        start = config.start.astimezone(timezone.utc)
        end = config.end.astimezone(timezone.utc)
        if floor_interval(start, config.interval) != start:
            raise ValueError("Feature range start must align to the configured interval")
        if floor_interval(end, config.interval) != end:
            raise ValueError("Feature range end must align to the configured interval")

        events = self._prepare_events(config)
        label_counts: dict[CountKey, int] = defaultdict(int)
        for event in events:
            bucket = floor_interval(event.occurred_at, config.interval)
            label_counts[(event.cell_id, event.category, bucket)] += 1

        interval_starts: list[datetime] = []
        cursor = start
        while cursor < end:
            interval_starts.append(cursor)
            cursor += config.interval

        rows: list[dict[str, Any]] = []
        domain_set = set(config.domain_cells)
        for interval_start in interval_starts:
            visible_counts: dict[CountKey, int] = defaultdict(int)
            for event in events:
                event_bucket = floor_interval(event.occurred_at, config.interval)
                if event_bucket < interval_start and event.received_at < interval_start:
                    visible_counts[(event.cell_id, event.category, event_bucket)] += 1

            hour_fraction = interval_start.hour + interval_start.minute / 60
            hour_angle = 2 * math.pi * hour_fraction / 24
            day_angle = 2 * math.pi * interval_start.weekday() / 7

            for category in config.categories:
                for cell_id in config.domain_cells:
                    def lag(offset: int, *, target_cell: str = cell_id) -> int:
                        bucket = interval_start - config.interval * offset
                        return visible_counts[(target_cell, category, bucket)]

                    last_seven = [lag(offset) for offset in range(1, 8)]
                    last_fourteen = [lag(offset) for offset in range(1, 15)]
                    recent_three = sum(last_fourteen[:3]) / 3
                    previous_three = sum(last_fourteen[3:6]) / 3
                    neighbors = set(h3.grid_disk(cell_id, 1)) - {cell_id}
                    neighbors &= domain_set
                    neighbor_lag = sum(lag(1, target_cell=neighbor) for neighbor in neighbors)

                    row: dict[str, Any] = {
                        "schema_version": "2.0.0",
                        "tenant_id": config.tenant_id,
                        "cell_id": cell_id,
                        "interval_start": utc_text(interval_start),
                        "category": category,
                        "event_count": label_counts[(cell_id, category, interval_start)],
                        "lag_1": lag(1),
                        "lag_2": lag(2),
                        "lag_7": lag(7),
                        "lag_14": lag(14),
                        "rolling_7_mean": sum(last_seven) / 7,
                        "rolling_14_mean": sum(last_fourteen) / 14,
                        "neighbor_lag_1": float(neighbor_lag),
                        "recent_trend": recent_three - previous_three,
                        "hour_sin": math.sin(hour_angle),
                        "hour_cos": math.cos(hour_angle),
                        "day_of_week_sin": math.sin(day_angle),
                        "day_of_week_cos": math.cos(day_angle),
                        "coverage_ratio": config.coverage_ratio,
                        "data_as_of": utc_text(interval_start - timedelta(seconds=1)),
                    }
                    validate_contract("feature-row.schema.json", row)
                    rows.append(row)
        return rows

    def write_parquet(
        self,
        config: FeatureBuildConfig,
        output_path: Path,
        manifest_path: Path,
        *,
        source_versions: dict[str, str],
        category_map_version: str,
        replay_input_path: Path,
        generation_command: list[str],
    ) -> dict[str, Any]:
        rows = self.build_rows(config)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(rows).sort(["tenant_id", "interval_start", "category", "cell_id"])
        frame.write_parquet(output_path, compression="zstd", statistics=True)

        repository_root = Path(__file__).resolve().parents[2]
        try:
            code_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            working_tree_dirty = bool(
                subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=repository_root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
        except (OSError, subprocess.CalledProcessError):
            code_commit = "unknown"
            working_tree_dirty = True

        feature_checksum = sha256_file(output_path)
        replay_checksum = sha256_file(replay_input_path)
        interval_minutes = int(config.interval.total_seconds() // 60)
        generation_text = subprocess.list2cmdline(generation_command)
        dataset_identity = {
            "artifact_sha256": feature_checksum,
            "category_map_version": category_map_version,
            "h3_resolution": config.h3_resolution,
            "interval_minutes": interval_minutes,
            "replay_input_sha256": replay_checksum,
            "source_versions": source_versions,
            "tenant_id": config.tenant_id,
        }
        dataset_digest = hashlib.sha256(
            json.dumps(dataset_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        manifest = {
            "schema_version": "1.0.0",
            "tenant_id": config.tenant_id,
            "dataset_version": f"features-{dataset_digest[:24]}",
            "feature_schema_version": "2.0.0",
            "artifact": {
                "path": display_path(output_path, repository_root),
                "format": "parquet",
                "sha256": feature_checksum,
            },
            "source_versions": [
                {"source_id": source_id, "source_version": source_versions[source_id]}
                for source_id in config.source_ids
            ],
            "generated_at": utc_now(),
            "data_as_of": utc_text(config.end - config.interval - timedelta(seconds=1)),
            "interval_minutes": interval_minutes,
            "row_count": len(rows),
            "interval_start_min": utc_text(config.start),
            "interval_start_max": utc_text(config.end - config.interval),
            "categories": list(config.categories),
            "cell_count": len(config.domain_cells),
            "generation_command": generation_text,
            "code_version": code_commit,
            "parameters": {
                "h3_resolution": config.h3_resolution,
                "coverage_ratio": config.coverage_ratio,
                "category_map_version": category_map_version,
                "replay_input_sha256": replay_checksum,
                "accepted_event_count": self.store.event_count(
                    config.tenant_id, config.source_ids
                ),
                "working_tree_dirty": working_tree_dirty,
            },
        }
        validate_contract("feature-table-manifest.schema.json", manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest


def load_domain_cells(path: Path) -> tuple[str, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values: Iterable[object]
    if isinstance(payload, dict):
        values = payload.get("cells", [])
    elif isinstance(payload, list):
        values = payload
    else:
        raise ValueError("Domain file must contain a JSON list or an object with a cells list")
    cells = tuple(sorted({str(value).lower() for value in values}))
    if not cells:
        raise ValueError("Domain file contains no H3 cells")
    return cells
