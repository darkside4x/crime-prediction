"""Fail-closed tests for the mobile WebM transcoder boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.video.errors import VideoPipelineError
from src.data.video.transcode import WEBM_EBML_SIGNATURE, FfmpegWebmTranscoder


def _webm(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(WEBM_EBML_SIGNATURE + b"synthetic-webm-payload")
    return path


def _probe_result(*, duration: float = 20.0, width: int = 1280, height: int = 720):
    return SimpleNamespace(
        stdout=json.dumps(
            {
                "format": {"duration": str(duration), "format_name": "matroska,webm"},
                "streams": [
                    {
                        "width": width,
                        "height": height,
                        "avg_frame_rate": "30/1",
                    }
                ],
            }
        )
    )


def test_transcoder_rejects_invalid_container_without_running_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "invalid.webm"
    source.write_bytes(b"not-a-webm-container")

    def unexpected_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("ffmpeg must not receive an invalid container")

    monkeypatch.setattr("src.data.video.transcode.subprocess.run", unexpected_run)

    with pytest.raises(VideoPipelineError) as caught:
        FfmpegWebmTranscoder().transcode(source, tmp_path / "output.mp4")

    assert caught.value.code == "video_corrupt"
    assert caught.value.retryable is False
    assert not (tmp_path / "output.mp4").exists()


def test_transcoder_uses_bounded_argv_without_shell_interpretation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _webm(tmp_path / "capture;echo-not-executed" / "input.webm")
    destination = tmp_path / "output;still-an-argv-value.mp4"
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[0] == "ffprobe":
            marker = command.index("-protocol_whitelist")
            assert command[marker + 1] == "file,pipe"
            return _probe_result()
        observed["command"] = command
        observed["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("src.data.video.transcode.subprocess.run", fake_run)

    FfmpegWebmTranscoder(timeout_seconds=17).transcode(source, destination)

    command = observed["command"]
    kwargs = observed["kwargs"]
    assert isinstance(command, list)
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == str(source.resolve())
    assert command[-1] == str(destination.resolve())
    assert "-nostdin" in command
    marker = command.index("-protocol_whitelist")
    assert command[marker + 1] == "file,pipe"
    assert kwargs == {"check": False, "capture_output": True, "timeout": 17}
    assert "-t" in command
    assert "-fs" in command
    assert "-threads" in command
    assert destination.is_file()


def test_transcoder_deletes_partial_output_when_ffmpeg_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _webm(tmp_path / "input.webm")
    destination = tmp_path / "output.mp4"

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        if command[0] == "ffprobe":
            return _probe_result()
        Path(command[-1]).write_bytes(b"partial-sensitive-media")
        return SimpleNamespace(returncode=1, stderr=b"provider detail must not leak")

    monkeypatch.setattr("src.data.video.transcode.subprocess.run", fake_run)

    with pytest.raises(VideoPipelineError) as caught:
        FfmpegWebmTranscoder().transcode(source, destination)

    assert caught.value.code == "video_transcode_failed"
    assert caught.value.retryable is False
    assert "provider detail" not in str(caught.value)
    assert not destination.exists()


def test_transcoder_timeout_is_retryable_and_deletes_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _webm(tmp_path / "input.webm")
    destination = tmp_path / "output.mp4"

    def timeout(command: list[str], **kwargs: object) -> object:
        del kwargs
        if command[0] == "ffprobe":
            return _probe_result()
        Path(command[-1]).write_bytes(b"partial-sensitive-media")
        raise subprocess.TimeoutExpired(command, timeout=3)

    monkeypatch.setattr("src.data.video.transcode.subprocess.run", timeout)

    with pytest.raises(VideoPipelineError) as caught:
        FfmpegWebmTranscoder(timeout_seconds=3).transcode(source, destination)

    assert caught.value.code == "video_transcode_failed"
    assert caught.value.retryable is True
    assert not destination.exists()


@pytest.mark.parametrize(
    ("source_name", "destination_name", "expected_code"),
    [
        ("input.mp4", "output.mp4", "video_type_invalid"),
        ("input.webm", "output.webm", "video_type_invalid"),
        ("missing.webm", "output.mp4", "video_path_invalid"),
    ],
)
def test_transcoder_rejects_invalid_paths_before_launching_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_name: str,
    destination_name: str,
    expected_code: str,
) -> None:
    source = tmp_path / source_name
    if source_name != "missing.webm":
        _webm(source)

    def unexpected_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("ffmpeg must not receive an invalid path")

    monkeypatch.setattr("src.data.video.transcode.subprocess.run", unexpected_run)

    with pytest.raises(VideoPipelineError) as caught:
        FfmpegWebmTranscoder().transcode(source, tmp_path / destination_name)

    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("duration", "width", "height", "expected_code"),
    [
        (31.0, 1280, 720, "video_duration_invalid"),
        (20.0, 3840, 2160, "video_dimensions_invalid"),
    ],
)
def test_transcoder_rejects_expensive_media_before_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duration: float,
    width: int,
    height: int,
    expected_code: str,
) -> None:
    source = _webm(tmp_path / "input.webm")

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        assert command[0] == "ffprobe"
        return _probe_result(duration=duration, width=width, height=height)

    monkeypatch.setattr("src.data.video.transcode.subprocess.run", fake_run)
    with pytest.raises(VideoPipelineError) as caught:
        FfmpegWebmTranscoder().transcode(source, tmp_path / "output.mp4")

    assert caught.value.code == expected_code
    assert not (tmp_path / "output.mp4").exists()
