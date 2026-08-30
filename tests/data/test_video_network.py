from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.video.capture import CameraConnection, FfmpegSegmenter
from src.data.video.errors import VideoPipelineError
from src.data.video.live import FfmpegHlsCapture, HlsSourceDefinition
from src.data.video.network import validate_public_media_url


def test_public_media_url_accepts_only_global_addresses() -> None:
    parsed = validate_public_media_url(
        "https://camera.example/live.m3u8",
        allowed_schemes={"https"},
        resolver=lambda _host, _port: {"8.8.8.8", "2606:4700:4700::1111"},
    )
    assert parsed.hostname == "camera.example"


@pytest.mark.parametrize(
    "url,address",
    [
        ("https://localhost/live.m3u8", "127.0.0.1"),
        ("https://camera.example/live.m3u8", "10.1.2.3"),
        ("https://camera.example/live.m3u8", "169.254.169.254"),
        ("https://camera.example:444/live.m3u8", "8.8.8.8"),
        ("http://camera.example/live.m3u8", "8.8.8.8"),
    ],
)
def test_public_media_url_rejects_private_metadata_and_bad_ports(
    url: str, address: str
) -> None:
    with pytest.raises(VideoPipelineError) as caught:
        validate_public_media_url(
            url,
            allowed_schemes={"https"},
            resolver=lambda _host, _port: {address},
        )
    assert caught.value.code == "camera_endpoint_invalid"


def test_allowlisted_hls_source_fails_closed_when_dns_is_private() -> None:
    capture = FfmpegHlsCapture(
        sources={
            "demo": HlsSourceDefinition(
                key="demo",
                name="Demo",
                url="https://camera.example/live.m3u8",
                attribution="Demo",
            )
        },
        address_resolver=lambda _host, _port: {"192.168.1.9"},
    )
    with pytest.raises(VideoPipelineError) as caught:
        capture.capture("demo", Path("unused.mp4"), duration_seconds=5)
    assert caught.value.code == "live_source_configuration_invalid"


def test_ffmpeg_hls_capture_has_a_minimal_protocol_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        observed.extend(command)
        Path(command[-1]).write_bytes(b"segment")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("src.data.video.capture.subprocess.run", fake_run)
    destination = tmp_path / "segment.mp4"
    FfmpegSegmenter("ffmpeg").capture(
        CameraConnection("hls", "https://camera.example/live.m3u8"),
        destination,
        duration_seconds=10,
    )
    marker = observed.index("-protocol_whitelist")
    assert observed[marker + 1] == "https,tls,tcp"
    assert "file" not in observed[marker + 1].split(",")
    assert observed[observed.index("-fs") + 1] == str(8 * 1024 * 1024)
