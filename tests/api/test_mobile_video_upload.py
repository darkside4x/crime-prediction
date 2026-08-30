"""API integration tests for authenticated mobile WebM uploads."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import reka
from src.api.app import create_app
from src.api.settings import Settings
from src.data.store import IngestionStore
from src.data.video import DictLocationResolver, FakeRekaVisionProvider
from src.data.video.broker import DatabaseJobBroker
from src.data.video.errors import VideoPipelineError
from src.data.video.service import VideoPipelineService
from src.data.video.store import VideoStore
from src.data.video.transcode import WEBM_EBML_SIGNATURE

TENANT = "00000000-0000-4000-8000-000000000001"
SOURCE = "20000000-0000-4000-8000-000000000001"
HEADERS = {
    "Authorization": "Bearer demo-token-one",
    "Idempotency-Key": "mobile-upload-0001",
}
FORM = {
    "source_id": SOURCE,
    "captured_start": "2026-08-30T10:00:00Z",
    "captured_end": "2026-08-30T10:01:00Z",
    "consent_confirmed": "true",
}
WEBM = WEBM_EBML_SIGNATURE + b"synthetic-mobile-capture"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"synthetic-converted-capture"


class SixtySecondInspector:
    def duration_seconds(self, path: Path) -> float:
        assert path.suffix == ".mp4"
        return 60.0


@dataclass
class RecordingScanner:
    suffixes: list[str] = field(default_factory=list)
    reject_webm: bool = False

    def scan(self, path: Path) -> None:
        assert path.is_file()
        self.suffixes.append(path.suffix)
        if self.reject_webm and path.suffix == ".webm":
            raise VideoPipelineError(
                "malware_detected", "Uploaded media failed malware scanning"
            )


@dataclass
class RecordingTranscoder:
    output: bytes = MP4
    calls: list[tuple[Path, Path]] = field(default_factory=list)

    def transcode(self, source: Path, destination: Path) -> None:
        assert source.read_bytes().startswith(WEBM_EBML_SIGNATURE)
        self.calls.append((source, destination))
        destination.write_bytes(self.output)


class FailingTranscoder:
    calls = 0

    def transcode(self, source: Path, destination: Path) -> None:
        self.calls += 1
        destination.write_bytes(b"partial-output")
        raise VideoPipelineError(
            "video_transcode_failed",
            "Mobile video conversion could not be completed",
            retryable=True,
        )


def _client(
    tmp_path: Path,
    *,
    scanner: RecordingScanner,
    transcoder: object,
    max_upload_bytes: int = 1024,
) -> tuple[TestClient, VideoStore, Path]:
    media_root = tmp_path / "restricted-media"
    ingestion = IngestionStore(tmp_path / "state.sqlite3")
    store = VideoStore(ingestion)
    service = VideoPipelineService(
        store,
        FakeRekaVisionProvider(),
        DictLocationResolver({}),
        media_root=media_root,
        max_upload_bytes=max_upload_bytes,
        media_inspector=SixtySecondInspector(),
        media_scanner=scanner,
    )
    app = create_app(
        provider=reka.FakeRekaProvider(),
        settings=Settings(runtime_dir=tmp_path / "runtime"),
        video_service=service,
        video_broker=DatabaseJobBroker(store),
        media_transcoder=transcoder,
    )
    return TestClient(app), store, media_root


def _upload(client: TestClient, *, headers: dict[str, str] | None = None):
    return client.post(
        "/v1/video-assets/uploads",
        data=FORM,
        files={"file": ("mobile-capture.webm", WEBM, "video/webm; codecs=vp8")},
        headers=headers or HEADERS,
    )


def test_mobile_webm_upload_is_scanned_converted_and_persisted_as_mp4(
    tmp_path: Path,
) -> None:
    scanner = RecordingScanner()
    transcoder = RecordingTranscoder()
    client, store, media_root = _client(
        tmp_path, scanner=scanner, transcoder=transcoder
    )

    response = _upload(client)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "queued"
    assert body["stage"] == "accepted"
    asset = store.get_asset(TENANT, body["asset_id"])
    assert asset["content_type"] == "video/mp4"
    assert scanner.suffixes == [".webm", ".mp4"]
    assert len(transcoder.calls) == 1
    assert not list(media_root.rglob("*.webm"))
    converted = list(media_root.rglob("*.mp4"))
    assert len(converted) == 1
    assert converted[0].read_bytes() == MP4


def test_mobile_webm_idempotent_replay_does_not_transcode_or_leak_temp_files(
    tmp_path: Path,
) -> None:
    scanner = RecordingScanner()
    transcoder = RecordingTranscoder()
    client, _, media_root = _client(
        tmp_path, scanner=scanner, transcoder=transcoder
    )

    first = _upload(client)
    replay = _upload(client)

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert len(transcoder.calls) == 1
    assert scanner.suffixes == [".webm", ".mp4"]
    assert not list(media_root.rglob("*.webm"))
    assert len(list(media_root.rglob("*.mp4"))) == 1


def test_mobile_webm_transcode_failure_is_typed_and_cleans_all_media(
    tmp_path: Path,
) -> None:
    scanner = RecordingScanner()
    transcoder = FailingTranscoder()
    client, store, media_root = _client(
        tmp_path, scanner=scanner, transcoder=transcoder
    )

    response = _upload(client)

    assert response.status_code == 503
    assert response.json()["code"] == "video_transcode_failed"
    assert response.json()["retryable"] is True
    assert transcoder.calls == 1
    assert scanner.suffixes == [".webm"]
    assert store.tenant_asset_bytes(TENANT) == 0
    assert not list(media_root.rglob("*.webm"))
    assert not list(media_root.rglob("*.mp4"))


def test_mobile_webm_is_scanned_before_the_decoder_runs(tmp_path: Path) -> None:
    scanner = RecordingScanner(reject_webm=True)
    transcoder = RecordingTranscoder()
    client, store, media_root = _client(
        tmp_path, scanner=scanner, transcoder=transcoder
    )

    response = _upload(client)

    assert response.status_code == 422
    assert response.json()["code"] == "malware_detected"
    assert transcoder.calls == []
    assert scanner.suffixes == [".webm"]
    assert store.tenant_asset_bytes(TENANT) == 0
    assert not list(media_root.rglob("*.webm"))
    assert not list(media_root.rglob("*.mp4"))


def test_mobile_webm_rejects_oversized_converted_output_and_cleans_it(
    tmp_path: Path,
) -> None:
    scanner = RecordingScanner()
    transcoder = RecordingTranscoder(output=MP4 + b"x" * 128)
    client, store, media_root = _client(
        tmp_path,
        scanner=scanner,
        transcoder=transcoder,
        max_upload_bytes=64,
    )

    response = _upload(client)

    assert response.status_code == 413
    assert response.json()["code"] == "video_size_invalid"
    assert scanner.suffixes == [".webm"]
    assert len(transcoder.calls) == 1
    assert store.tenant_asset_bytes(TENANT) == 0
    assert not list(media_root.rglob("*.webm"))
    assert not list(media_root.rglob("*.mp4"))
