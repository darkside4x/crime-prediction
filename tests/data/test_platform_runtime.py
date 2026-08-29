from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.data.store import IngestionStore
from src.data.video import (
    DatabaseJobBroker,
    DictLocationResolver,
    FakeRekaVisionProvider,
    InMemoryCoverageTelemetry,
    JobMessage,
    S3MediaStorage,
    SqsJobBroker,
    VideoJobWorker,
    VideoPipelineService,
    VideoStore,
)
from src.data.video.capture import LiveCaptureWorker
from src.data.video.coverage import CoverageObservation, persist_measured_snapshot
from src.data.video.errors import VideoPipelineError

TENANT = "11111111-1111-4111-8111-111111111111"
SOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source() -> dict:
    return {
        "schema_version": "1.0.0",
        "tenant_id": TENANT,
        "source_id": SOURCE,
        "name": "Durable worker test",
        "mode": "recorded_video",
        "status": "active",
        "timezone": "UTC",
        "location_ref": f"secret://locations/{SOURCE}",
        "connection": {"transport": "uploaded_asset"},
        "retention_policy_days": 30,
        "created_at": "2026-01-01T00:00:00Z",
    }


def _setup(tmp_path: Path, *, fail_upload: bool = False):
    root = tmp_path / "restricted"
    root.mkdir()
    ingestion = IngestionStore(tmp_path / "state.sqlite3")
    store = VideoStore(ingestion)
    provider = FakeRekaVisionProvider(
        proposals=[{"offset_seconds": 1, "category": "property", "confidence": 0.8}],
        fail_operations={"upload"} if fail_upload else set(),
    )

    class Inspector:
        def duration_seconds(self, path: Path) -> float:
            return 30.0

    service = VideoPipelineService(
        store,
        provider,
        DictLocationResolver(
            {(TENANT, f"secret://locations/{SOURCE}"): {"latitude": 12.9, "longitude": 77.5}}
        ),
        media_root=root,
        media_inspector=Inspector(),
    )
    service.register_recorded_source(_source(), authenticated_tenant_id=TENANT)
    path = root / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"safe-test-media" * 8)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    asset = service.accept_upload(
        authenticated_tenant_id=TENANT,
        source_id=SOURCE,
        path=path,
        content_type="video/mp4",
        captured_start=_time(start),
        captured_end=_time(start + timedelta(seconds=30)),
        duration_seconds=30,
        consent_confirmed=True,
    )
    return store, provider, service, asset


def test_separate_workers_resume_persisted_chain_after_restart(tmp_path: Path) -> None:
    store, provider, service, asset = _setup(tmp_path)
    upload = store.enqueue(TENANT, asset["asset_id"], "upload")
    broker = DatabaseJobBroker(store)
    broker.publish(JobMessage(TENANT, upload["job_id"], "upload"))

    upload_worker = VideoJobWorker(
        store=store, broker=broker, service=service, operations=("upload",), worker_id="upload-1"
    )
    assert upload_worker.poll_once()[0].state == "completed"
    assert len([call for call in provider.calls if call[0] == "upload"]) == 1

    # Re-open both stores to prove queued state is not tied to worker memory.
    restarted_store = VideoStore(IngestionStore(tmp_path / "state.sqlite3"))
    restarted_broker = DatabaseJobBroker(restarted_store)
    restarted_service = VideoPipelineService(
        restarted_store,
        provider,
        service.location_resolver,
        media_root=tmp_path / "restricted",
        media_inspector=service.media_inspector,
    )
    index_worker = VideoJobWorker(
        store=restarted_store,
        broker=restarted_broker,
        service=restarted_service,
        operations=("index",),
        worker_id="index-1",
    )
    telemetry = InMemoryCoverageTelemetry()
    analyze_worker = VideoJobWorker(
        store=restarted_store,
        broker=restarted_broker,
        service=restarted_service,
        operations=("analyze",),
        worker_id="analyze-1",
        telemetry=telemetry,
    )
    assert index_worker.poll_once()[0].state == "completed"
    assert analyze_worker.poll_once()[0].state == "completed"
    assert len(restarted_store.list_candidates(TENANT)) == 1
    assert restarted_store.job_metrics(TENANT) == {"completed": 3}
    assert telemetry.observations[0].detector_available is True


def test_retry_uses_persisted_exponential_backoff(tmp_path: Path) -> None:
    store, _, service, asset = _setup(tmp_path, fail_upload=True)
    job = store.enqueue(TENANT, asset["asset_id"], "upload")
    worker = VideoJobWorker(
        store=store,
        broker=DatabaseJobBroker(store),
        service=service,
        operations=("upload",),
        worker_id="upload-1",
    )
    result = worker.poll_once()[0]
    persisted = store.get_job(TENANT, job["job_id"])
    assert result.state == "retry"
    assert persisted["state"] == "retry"
    assert persisted["last_error_code"] == "reka_unavailable"
    assert worker.poll_once() == []


class FakeS3:
    def __init__(self) -> None:
        self.uploads: list[tuple] = []
        self.downloads: list[tuple] = []
        self.deletes: list[dict] = []

    def upload_file(self, *args, **kwargs):
        self.uploads.append((args, kwargs))

    def download_file(self, bucket, key, target):
        self.downloads.append((bucket, key, target))
        Path(target).write_bytes(b"materialized")

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)


def test_s3_storage_is_tenant_prefixed_kms_encrypted_and_reference_safe(tmp_path: Path) -> None:
    client = FakeS3()
    storage = S3MediaStorage(
        bucket="restricted", kms_key_id="alias/video", region_name="ap-south-1", client=client
    )
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media")
    ref = storage.store(path, tenant_id=TENANT, asset_id=SOURCE, sha256="a" * 64)
    args, kwargs = client.uploads[0]
    assert args[2].startswith(f"tenants/{TENANT}/video-assets/{SOURCE}/")
    assert kwargs["ExtraArgs"]["ServerSideEncryption"] == "aws:kms"
    assert kwargs["ExtraArgs"]["SSEKMSKeyId"] == "alias/video"
    assert "restricted" not in ref and args[2] not in ref
    with (
        pytest.raises(VideoPipelineError),
        storage.materialize(
            ref,
            tenant_id="22222222-2222-4222-8222-222222222222",
            asset_id=SOURCE,
        ),
    ):
        pass


def test_coverage_is_measured_available_seconds_over_expected(tmp_path: Path) -> None:
    _, _, service, _ = _setup(tmp_path)
    telemetry = InMemoryCoverageTelemetry()
    telemetry.record(
        CoverageObservation(
            TENANT, SOURCE, "2026-01-01T00:00:00Z", 300,
            connected=True, frame_processable=True, detector_available=True,
            processing_latency_ms=250,
        )
    )
    telemetry.record(
        CoverageObservation(
            TENANT, SOURCE, "2026-01-01T00:05:00Z", 300,
            connected=True, frame_processable=True, detector_available=False,
            reka_available=False,
        )
    )
    snapshot = persist_measured_snapshot(
        telemetry,
        service,
        tenant_id=TENANT,
        source_id=SOURCE,
        interval_start="2026-01-01T00:00:00Z",
        interval_end="2026-01-01T00:10:00Z",
    )
    assert snapshot["detector_available_seconds"] == 300
    assert snapshot["expected_seconds"] == 600
    assert snapshot["coverage_ratio"] == 0.5
    assert "reka_unavailable" in snapshot["degraded_reason_codes"]


def test_hls_live_capture_creates_bounded_segment_and_durable_upload_job(tmp_path: Path) -> None:
    store, _, service, _ = _setup(tmp_path)
    live_source = {
        **_source(),
        "source_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "mode": "live_camera",
        "connection": {
            "transport": "hls",
            "endpoint_ref": "secret://camera/endpoint",
            "credential_ref": "secret://camera/credentials",
        },
    }
    service.register_live_source(live_source, authenticated_tenant_id=TENANT)

    class Secrets:
        def resolve_json(self, ref: str) -> dict:
            if ref.endswith("endpoint"):
                return {"stream_url": "https://camera.example/approved.m3u8"}
            return {}

    class Segmenter:
        def capture(self, connection, output: Path, *, duration_seconds: int) -> None:
            assert connection.transport == "hls"
            assert duration_seconds == 30
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"segment" * 20)

    telemetry = InMemoryCoverageTelemetry()
    result = LiveCaptureWorker(
        store=store,
        service=service,
        broker=DatabaseJobBroker(store),
        secrets=Secrets(),
        telemetry=telemetry,
        segmenter=Segmenter(),
        spool_root=tmp_path / "restricted",
        segment_seconds=30,
    ).capture_once(TENANT, live_source["source_id"])
    assert result and result["status"] == "queued"
    assert store.get_asset(TENANT, result["asset_id"])["kind"] == "live_segment"
    assert telemetry.observations[-1].connected is True


class FakeSqs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deleted: list[dict] = []
        self.visibility: list[dict] = []
        self.messages: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)

    def receive_message(self, **kwargs):
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs):
        self.visibility.append(kwargs)

    def get_queue_attributes(self, **kwargs):
        return {"Attributes": {"ApproximateNumberOfMessages": "2", "ApproximateNumberOfMessagesNotVisible": "1"}}


def test_sqs_delivery_is_minimal_retryable_and_explicitly_dead_lettered() -> None:
    client = FakeSqs()
    broker = SqsJobBroker(
        queue_url="https://sqs.example/jobs",
        dead_letter_queue_url="https://sqs.example/jobs-dlq",
        region_name="ap-south-1",
        wait_seconds=0,
        client=client,
    )
    message = JobMessage(TENANT, SOURCE, "upload")
    broker.publish(message)
    assert set(json.loads(client.sent[0]["MessageBody"])) == {
        "tenant_id", "job_id", "operation"
    }
    client.messages = [
        {
            "Body": message.body(),
            "ReceiptHandle": "receipt-1",
            "Attributes": {"ApproximateReceiveCount": "3"},
        }
    ]
    delivery = broker.receive(operations=("upload",))[0]
    broker.retry(delivery, delay_seconds=8)
    assert client.visibility[-1]["VisibilityTimeout"] == 8
    broker.dead_letter(delivery, error_code="reka_timeout")
    assert client.sent[-1]["QueueUrl"].endswith("jobs-dlq")
    assert client.deleted[-1]["ReceiptHandle"] == "receipt-1"
    assert broker.depth() == 3
