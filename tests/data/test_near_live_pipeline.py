from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient

from src.api import reka
from src.api.app import DEMO_HLS_LOCATION_REF, create_app
from src.api.settings import Settings
from src.api.tenancy import DEMO_TENANT_ONE
from src.data.store import IngestionStore
from src.data.video import DictLocationResolver, FakeRekaVisionProvider, VideoPipelineService, VideoStore
from src.data.video.live import CapturedSegment, DEFAULT_HLS_SOURCES, HlsSourceDefinition


class Inspector:
    def duration_seconds(self, path: Path) -> float:
        return 10.0


class FakeCapture:
    definition = HlsSourceDefinition(
        key="louisiana-dot-i20",
        name="Louisiana DOT test feed",
        url="https://example.invalid/playlist.m3u8",
        attribution="LADOTD / 511 Louisiana",
    )

    def source(self, key: str) -> HlsSourceDefinition:
        assert key == self.definition.key
        return self.definition

    def capture(self, key: str, destination: Path, *, duration_seconds: int) -> CapturedSegment:
        assert key == self.definition.key
        assert duration_seconds == 10
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"bounded-live-segment" * 8)
        return CapturedSegment(destination, "2026-08-30T00:00:00Z")


def test_default_live_feed_uses_the_official_catalog_video_url() -> None:
    source = DEFAULT_HLS_SOURCES["louisiana-dot-i20"]
    assert source.url == (
        "https://ITSStreamingBR2.dotd.la.gov/public/"
        "shr-cam-002.streams/playlist.m3u8"
    )
    assert source.catalog_api_url == "https://511la.org/api/v2/get/cameras"
    assert source.catalog_source_id == "101"
    assert source.catalog_view_id == "2206"


def test_allowlisted_hls_capture_reaches_validated_human_review(tmp_path: Path) -> None:
    ingestion = IngestionStore(tmp_path / "restricted.sqlite3")
    video_store = VideoStore(ingestion)
    vision = FakeRekaVisionProvider(
        proposals=[{"offset_seconds": 3, "category": "traffic_safety", "confidence": 0.7}]
    )
    resolver = DictLocationResolver(
        {(DEMO_TENANT_ONE, DEMO_HLS_LOCATION_REF): {"latitude": 32.46, "longitude": -93.83}}
    )
    video_service = VideoPipelineService(
        video_store,
        vision,
        resolver,
        media_root=tmp_path / "media",
        media_inspector=Inspector(),
    )
    app = create_app(
        provider=reka.FakeRekaProvider(),
        settings=Settings(
            app_environment="test",
            runtime_dir=tmp_path / "runtime",
            near_live_capture_seconds=10,
            reka_index_poll_seconds=0,
            reka_index_max_polls=2,
        ),
        video_service=video_service,
        hls_capture=FakeCapture(),  # type: ignore[arg-type]
    )
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    admin = {
        "Authorization": "Bearer demo-token-one",
        "Idempotency-Key": "near-live-test-0001",
    }

    started = client.post(
        "/v1/demo/near-live-cctv/captures",
        json={"source_key": "louisiana-dot-i20", "duration_seconds": 10},
        headers=admin,
    )
    assert started.status_code == 202, started.text
    for _ in range(50):
        run = client.get(
            f"/v1/ingestion/runs/{started.json()['run_id']}",
            headers={"Authorization": "Bearer demo-token-one"},
        )
        if run.json()["state"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert run.status_code == 200
    assert run.json()["state"] == "completed"
    assert run.json()["label"] == "near-live CCTV segment"
    assert run.json()["candidate_count"] == 1

    listing = client.get(
        "/v1/candidate-detections",
        headers={"Authorization": "Bearer demo-token-one"},
    )
    candidate = next(
        item for item in listing.json()["items"] if item.get("asset_id") == run.json()["asset_id"]
    )
    assert "evidence_ref" not in candidate
    assert candidate["record_type"] == "unconfirmed_candidate_detection"

    reviewed = client.post(
        f"/v1/candidate-detections/{candidate['detection_id']}/review",
        json={"decision": "confirmed", "confirmed_category": "traffic_safety"},
        headers={
            "Authorization": "Bearer demo-token-one",
            "Idempotency-Key": "near-live-review-0001",
        },
    )
    assert reviewed.status_code == 201, reviewed.text
    assert ingestion.event_count(DEMO_TENANT_ONE) == 1

    denied = client.get(
        f"/v1/ingestion/runs/{started.json()['run_id']}",
        headers={"Authorization": "Bearer demo-token-two"},
    )
    assert denied.status_code == 404
