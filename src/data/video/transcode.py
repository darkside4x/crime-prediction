"""Bounded WebM-to-MP4 conversion for mobile-browser captures."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .errors import VideoPipelineError

WEBM_EBML_SIGNATURE = b"\x1a\x45\xdf\xa3"


@dataclass(frozen=True)
class FfmpegWebmTranscoder:
    """Convert one already-size-bounded WebM capture into a portable MP4.

    Paths are supplied as separate argv values and never interpreted by a
    shell. The caller remains responsible for placing both files inside the
    restricted tenant spool and deleting them after acceptance or failure.
    """

    timeout_seconds: int = 180
    probe_timeout_seconds: int = 20
    max_duration_seconds: int = 30
    max_input_bytes: int = 100 * 1024 * 1024
    max_output_bytes: int = 100 * 1024 * 1024
    max_width: int = 1920
    max_height: int = 1080
    max_frames_per_second: float = 60.0

    def _validate_media_bounds(self, source: Path) -> None:
        if source.stat().st_size > self.max_input_bytes:
            raise VideoPipelineError(
                "video_size_invalid", "Mobile capture exceeds the conversion size limit"
            )
        command = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration,format_name:stream=width,height,avg_frame_rate",
            "-of",
            "json",
            str(source),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.probe_timeout_seconds,
            )
            payload = json.loads(completed.stdout)
            format_data = payload["format"]
            streams = payload["streams"]
            if len(streams) != 1:
                raise ValueError("missing video stream")
            stream = streams[0]
            formats = set(str(format_data["format_name"]).split(","))
            duration = float(format_data["duration"])
            width = int(stream["width"])
            height = int(stream["height"])
            frames_per_second = float(Fraction(str(stream["avg_frame_rate"])))
        except (
            OSError,
            subprocess.SubprocessError,
            KeyError,
            TypeError,
            ValueError,
            ZeroDivisionError,
            json.JSONDecodeError,
        ) as error:
            raise VideoPipelineError(
                "video_probe_failed", "Mobile video metadata could not be validated"
            ) from error
        if "webm" not in formats and "matroska" not in formats:
            raise VideoPipelineError(
                "video_corrupt", "Uploaded media is not a WebM container"
            )
        if (
            not math.isfinite(duration)
            or duration <= 0
            or duration > self.max_duration_seconds
        ):
            raise VideoPipelineError(
                "video_duration_invalid",
                f"Mobile capture must be no longer than {self.max_duration_seconds} seconds",
            )
        if (
            width <= 0
            or height <= 0
            or width > self.max_width
            or height > self.max_height
        ):
            raise VideoPipelineError(
                "video_dimensions_invalid",
                "Mobile capture dimensions exceed the conversion limit",
            )
        if (
            not math.isfinite(frames_per_second)
            or frames_per_second <= 0
            or frames_per_second > self.max_frames_per_second
        ):
            raise VideoPipelineError(
                "video_frame_rate_invalid",
                "Mobile capture frame rate exceeds the conversion limit",
            )

    def transcode(self, source: Path, destination: Path) -> None:
        source_path = source.resolve()
        destination_path = destination.resolve()
        if (
            source_path.suffix.lower() != ".webm"
            or destination_path.suffix.lower() != ".mp4"
        ):
            raise VideoPipelineError(
                "video_type_invalid",
                "Mobile conversion requires WebM input and MP4 output",
            )
        if not source_path.is_file():
            raise VideoPipelineError(
                "video_path_invalid", "Uploaded video is unavailable"
            )
        try:
            with source_path.open("rb") as handle:
                signature = handle.read(4)
        except OSError as error:
            raise VideoPipelineError(
                "video_path_invalid", "Uploaded video is unavailable"
            ) from error
        if signature != WEBM_EBML_SIGNATURE:
            raise VideoPipelineError(
                "video_corrupt", "WebM container signature is invalid"
            )
        self._validate_media_bounds(source_path)
        destination_path.unlink(missing_ok=True)
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-t",
            str(self.max_duration_seconds),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-fs",
            str(self.max_output_bytes),
            str(destination_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            destination_path.unlink(missing_ok=True)
            raise VideoPipelineError(
                "video_transcode_failed",
                "Mobile video conversion could not be completed",
                retryable=isinstance(error, subprocess.TimeoutExpired),
            ) from error
        if (
            completed.returncode != 0
            or not destination_path.is_file()
            or destination_path.stat().st_size > self.max_output_bytes
        ):
            destination_path.unlink(missing_ok=True)
            raise VideoPipelineError(
                "video_transcode_failed",
                "Mobile video conversion could not be completed",
            )
