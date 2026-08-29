"""Scheduled, unlabelled feature generation for operational future windows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from src.data.contracts import validate_contract
from src.data.video.store import VideoStore

from .builder import FeatureBuildConfig, FeatureBuilder, floor_interval, utc_text


class FutureFeatureBuilder:
    def __init__(self, video_store: VideoStore) -> None:
        self.video_store = video_store
        self.builder = FeatureBuilder(video_store.ingestion_store)

    def build_and_persist(
        self,
        config: FeatureBuildConfig,
        *,
        feature_snapshot_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build future predictors using only data received before the target.

        Coverage is taken from the latest completed measured source snapshots;
        a missing snapshot fails closed instead of silently becoming 1.0.
        """
        if config.end - config.start != config.interval:
            raise ValueError("An operational snapshot must contain exactly one future interval")
        target = config.start.astimezone(timezone.utc)
        coverage = self.video_store.latest_coverage_ratio(
            config.tenant_id, config.source_ids, utc_text(target)
        )
        labelled = self.builder.build_rows(replace(config, coverage_ratio=coverage))
        base_rows: list[dict[str, Any]] = []
        for row in labelled:
            future = {key: value for key, value in row.items() if key != "event_count"}
            future["schema_version"] = "1.0.0"
            base_rows.append(future)
        if feature_snapshot_version is None:
            digest = hashlib.sha256(
                json.dumps(base_rows, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            feature_snapshot_version = f"future-{digest[:24]}"
        rows: list[dict[str, Any]] = []
        for row in base_rows:
            row["feature_snapshot_version"] = feature_snapshot_version
            validate_contract("forecast-feature-row.schema.json", row)
            rows.append(row)
        self.video_store.put_future_snapshot(
            config.tenant_id, feature_snapshot_version, utc_text(target), rows
        )
        return rows


class ScheduledFeatureGenerator:
    """Scheduler-facing helper that generates the next aligned future window."""

    def __init__(self, builder: FutureFeatureBuilder, template: FeatureBuildConfig) -> None:
        self.builder = builder
        self.template = template

    def run(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        aligned = floor_interval(current, self.template.interval)
        target = aligned if current == aligned else aligned + self.template.interval
        config = replace(self.template, start=target, end=target + self.template.interval)
        return self.builder.build_and_persist(config)
