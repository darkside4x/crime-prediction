from __future__ import annotations

from datetime import datetime, timedelta, timezone

import h3

from src.data.store import IngestionStore
from src.data.video import DictLocationResolver, FakeRekaVisionProvider, VideoPipelineService, VideoStore
from src.features import FeatureBuildConfig, FutureFeatureBuilder, ScheduledFeatureGenerator


TENANT = "11111111-1111-4111-8111-111111111111"
SOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_scheduled_future_rows_are_unlabelled_measured_and_leakage_safe(tmp_path) -> None:
    ingestion = IngestionStore(tmp_path / "state.sqlite3")
    store = VideoStore(ingestion)
    service = VideoPipelineService(
        store,
        FakeRekaVisionProvider(),
        DictLocationResolver({(TENANT, "secret://locations/a"): {"latitude": 12.9716, "longitude": 77.5946}}),
        media_root=tmp_path,
        media_inspector=type("Inspector", (), {"duration_seconds": lambda self, path: 60.0})(),
    )
    source = {
        "schema_version": "1.0.0", "tenant_id": TENANT, "source_id": SOURCE,
        "name": "camera", "mode": "recorded_video", "status": "active", "timezone": "UTC",
        "location_ref": "secret://locations/a", "connection": {"transport": "uploaded_asset"},
        "retention_policy_days": 30, "created_at": "2026-01-01T00:00:00Z",
    }
    service.register_recorded_source(source, authenticated_tenant_id=TENANT)
    service.record_coverage(
        tenant_id=TENANT, source_id=SOURCE,
        interval_start="2026-01-01T00:00:00Z", interval_end="2026-01-01T01:00:00Z",
        connected_seconds=3000, processable_seconds=2700, detector_available_seconds=1800,
    )
    cell = h3.latlng_to_cell(12.9716, 77.5946, 8)
    template = FeatureBuildConfig(
        tenant_id=TENANT, source_ids=(SOURCE,),
        start=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
        interval=timedelta(hours=1), h3_resolution=8,
        domain_cells=(cell,), categories=("property",), coverage_ratio=1.0,
    )
    generator = ScheduledFeatureGenerator(FutureFeatureBuilder(store), template)
    rows_before = generator.run(datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc))
    assert len(rows_before) == 1
    assert "event_count" not in rows_before[0]
    assert rows_before[0]["coverage_ratio"] == 0.5
    assert rows_before[0]["interval_start"] == "2026-01-01T02:00:00Z"

    future_event = {
        "schema_version": "1.0.0", "tenant_id": TENANT, "source_id": SOURCE,
        "external_event_id": "arrived-later", "occurred_at": "2026-01-01T01:15:00Z",
        "received_at": "2026-01-01T02:30:00Z", "category": "property",
        "location": {"latitude": 12.9716, "longitude": 77.5946}, "attributes": {},
    }
    from src.data.service import _payload_hash
    ingestion.insert_event(future_event, _payload_hash(future_event))
    rows_after = generator.run(datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc))
    comparable = lambda row: {key: value for key, value in row.items() if key != "feature_snapshot_version"}
    assert comparable(rows_before[0]) == comparable(rows_after[0])
