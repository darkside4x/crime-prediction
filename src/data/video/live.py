"""Bounded capture adapter for explicitly allowlisted public HLS demo feeds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

from .errors import VideoPipelineError


LOUISIANA_DOT_HLS = (
    "https://ITSStreamingBR2.dotd.la.gov/public/"
    "shr-cam-002.streams/playlist.m3u8"
)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HlsSourceDefinition:
    key: str
    name: str
    url: str
    attribution: str


@dataclass(frozen=True)
class CapturedSegment:
    path: Path
    captured_start: str


DEFAULT_HLS_SOURCES = {
    "louisiana-dot-i20": HlsSourceDefinition(
        key="louisiana-dot-i20",
        name="Louisiana DOT I-20 public traffic camera",
        url=LOUISIANA_DOT_HLS,
        attribution="LADOTD / 511 Louisiana",
    )
}


class FfmpegHlsCapture:
    """Capture a short MP4 from a fixed HLS allowlist; never accepts arbitrary URLs."""

    def __init__(
        self,
        *,
        sources: dict[str, HlsSourceDefinition] | None = None,
        timeout_margin_seconds: int = 30,
    ) -> None:
        self.sources = dict(sources or DEFAULT_HLS_SOURCES)
        self.timeout_margin_seconds = timeout_margin_seconds

    def source(self, key: str) -> HlsSourceDefinition:
        try:
            source = self.sources[key]
        except KeyError as error:
            raise VideoPipelineError(
                "live_source_not_allowlisted", "The requested demo source is not allowlisted"
            ) from error
        parsed = urlparse(source.url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.path.endswith(".m3u8"):
            raise VideoPipelineError(
                "live_source_configuration_invalid", "The allowlisted HLS source is invalid"
            )
        return source

    def capture(self, key: str, destination: Path, *, duration_seconds: int) -> CapturedSegment:
        if not 5 <= duration_seconds <= 60:
            raise VideoPipelineError(
                "live_capture_duration_invalid", "Capture duration must be between 5 and 60 seconds"
            )
        source = self.source(key)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise VideoPipelineError(
                "ffmpeg_unavailable", "ffmpeg is required for near-live capture"
            )
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc)
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    source.url,
                    "-t",
                    str(duration_seconds),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(destination),
                ],
                check=True,
                capture_output=True,
                timeout=duration_seconds + self.timeout_margin_seconds,
            )
        except subprocess.TimeoutExpired as error:
            destination.unlink(missing_ok=True)
            raise VideoPipelineError(
                "live_capture_timeout", "The public camera did not produce a segment in time", retryable=True
            ) from error
        except subprocess.CalledProcessError as error:
            destination.unlink(missing_ok=True)
            raise VideoPipelineError(
                "live_capture_failed", "The public camera stream is currently unavailable", retryable=True
            ) from error
        if not destination.is_file():
            raise VideoPipelineError("live_capture_failed", "No media segment was produced", retryable=True)
        return CapturedSegment(path=destination, captured_start=_utc(started))
