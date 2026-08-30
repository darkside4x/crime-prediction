"""Bounded capture adapter for explicitly allowlisted public HLS demo feeds."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .errors import VideoPipelineError
from .network import AddressResolver, validate_public_media_url

LOUISIANA_DOT_HLS = (
    "https://ITSStreamingBR2.dotd.la.gov/public/shr-cam-002.streams/playlist.m3u8"
)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HlsSourceDefinition:
    key: str
    name: str
    url: str
    attribution: str
    catalog_api_url: str | None = None
    catalog_source_id: str | None = None
    catalog_view_id: str | None = None


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
        catalog_api_url="https://511la.org/api/v2/get/cameras",
        catalog_source_id="101",
        catalog_view_id="2206",
    )
}


class FfmpegHlsCapture:
    """Capture a short MP4 from a fixed HLS allowlist; never accepts arbitrary URLs."""

    def __init__(
        self,
        *,
        sources: dict[str, HlsSourceDefinition] | None = None,
        timeout_margin_seconds: int = 30,
        max_output_bytes: int = 8 * 1024 * 1024,
        address_resolver: AddressResolver | None = None,
    ) -> None:
        if max_output_bytes < 1024 * 1024:
            raise ValueError("HLS segment limit must be at least 1 MiB")
        self.sources = dict(sources or DEFAULT_HLS_SOURCES)
        self.timeout_margin_seconds = timeout_margin_seconds
        self.max_output_bytes = max_output_bytes
        self.address_resolver = address_resolver

    def source(self, key: str) -> HlsSourceDefinition:
        try:
            source = self.sources[key]
        except KeyError as error:
            raise VideoPipelineError(
                "live_source_not_allowlisted",
                "The requested demo source is not allowlisted",
            ) from error
        try:
            parsed = urlsplit(source.url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and (parsed.port is None or parsed.port == 443)
                and parsed.path.endswith(".m3u8")
            )
        except ValueError:
            valid = False
        if not valid:
            raise VideoPipelineError(
                "live_source_configuration_invalid",
                "The allowlisted HLS source is invalid",
            )
        return source

    def capture(
        self, key: str, destination: Path, *, duration_seconds: int
    ) -> CapturedSegment:
        if not 5 <= duration_seconds <= 60:
            raise VideoPipelineError(
                "live_capture_duration_invalid",
                "Capture duration must be between 5 and 60 seconds",
            )
        source = self.source(key)
        try:
            validate_public_media_url(
                source.url,
                allowed_schemes={"https"},
                resolver=self.address_resolver,
            )
        except VideoPipelineError as error:
            raise VideoPipelineError(
                "live_source_configuration_invalid",
                "The allowlisted HLS source is invalid",
            ) from error
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise VideoPipelineError(
                "ffmpeg_unavailable", "ffmpeg is required for near-live capture"
            )
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC)
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-protocol_whitelist",
                    "https,tls,tcp",
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
                    "-fs",
                    str(self.max_output_bytes),
                    str(destination),
                ],
                check=True,
                capture_output=True,
                timeout=duration_seconds + self.timeout_margin_seconds,
            )
        except subprocess.TimeoutExpired as error:
            destination.unlink(missing_ok=True)
            raise VideoPipelineError(
                "live_capture_timeout",
                "The public camera did not produce a segment in time",
                retryable=True,
            ) from error
        except subprocess.CalledProcessError as error:
            destination.unlink(missing_ok=True)
            raise VideoPipelineError(
                "live_capture_failed",
                "The public camera stream is currently unavailable",
                retryable=True,
            ) from error
        if (
            not destination.is_file()
            or destination.stat().st_size <= 0
            or destination.stat().st_size > self.max_output_bytes
        ):
            raise VideoPipelineError(
                "live_capture_failed", "No media segment was produced", retryable=True
            )
        return CapturedSegment(path=destination, captured_start=_utc(started))


class SimulatedVideoCapture:
    """Generate a bounded, clearly synthetic road segment for demo validation."""

    def __init__(self, *, timeout_margin_seconds: int = 20) -> None:
        self.timeout_margin_seconds = timeout_margin_seconds

    def capture(self, destination: Path, *, duration_seconds: int) -> CapturedSegment:
        if not 5 <= duration_seconds <= 30:
            raise VideoPipelineError(
                "simulated_capture_duration_invalid",
                "Simulated capture duration must be between 5 and 30 seconds",
            )
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise VideoPipelineError(
                "ffmpeg_unavailable", "ffmpeg is required for simulated capture"
            )
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc)
        road_filter = (
            "drawbox=x=0:y=350:w=1280:h=4:color=0xd9c9bd:t=fill,"
            "drawbox=x=0:y=230:w=1280:h=2:color=0x6c6264:t=fill,"
            "drawbox=x=0:y=470:w=1280:h=2:color=0x6c6264:t=fill,"
            "drawbox=x=80*t:y=275:w=126:h=54:color=0x8aa0ad:t=fill,"
            "drawbox=x=1100-62*t:y=385:w=118:h=52:color=0xb4795d:t=fill"
        )
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=0x171719:s=1280x720:r=12:d={duration_seconds}",
                    "-vf",
                    road_filter,
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
                "simulated_capture_timeout",
                "The simulated segment was not produced in time",
                retryable=True,
            ) from error
        except subprocess.CalledProcessError as error:
            destination.unlink(missing_ok=True)
            raise VideoPipelineError(
                "simulated_capture_failed",
                "The simulated segment could not be generated",
                retryable=True,
            ) from error
        if not destination.is_file():
            raise VideoPipelineError(
                "simulated_capture_failed",
                "No simulated media segment was produced",
                retryable=True,
            )
        return CapturedSegment(path=destination, captured_start=_utc(started))
