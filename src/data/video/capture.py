"""Secret-backed, bounded HLS/RTSP/ONVIF edge segmentation."""

from __future__ import annotations

import base64
import json
import math
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from .broker import JobBroker, JobMessage
from .coverage import CoverageObservation, CoverageTelemetry
from .errors import VideoPipelineError
from .service import VideoPipelineService


class SecretResolver(Protocol):
    def resolve_json(self, secret_ref: str) -> dict[str, Any]: ...


class AwsSecretsManagerResolver:
    """Resolves only explicitly mapped secret references; secret values are never logged."""

    def __init__(
        self,
        *,
        reference_map: dict[str, str],
        region_name: str,
        client: object | None = None,
    ) -> None:
        self.reference_map = dict(reference_map)
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover
                raise RuntimeError(
                    "Install the platform extra: pip install -e '.[platform]'"
                ) from error
            client = boto3.client("secretsmanager", region_name=region_name)
        self.client = client

    def resolve_json(self, secret_ref: str) -> dict[str, Any]:
        secret_id = self.reference_map.get(secret_ref)
        if secret_id is None:
            raise VideoPipelineError(
                "secret_ref_unknown", "Camera secret reference is not allowlisted"
            )
        try:
            response = self.client.get_secret_value(SecretId=secret_id)
            if "SecretString" in response:
                raw = response["SecretString"]
            else:
                raw = base64.b64decode(response["SecretBinary"]).decode("utf-8")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError("secret is not an object")
            return value
        except VideoPipelineError:
            raise
        except Exception as error:
            raise VideoPipelineError(
                "camera_secret_unavailable",
                "Camera connection secret was unavailable",
                retryable=True,
            ) from error


class AwsSecretsManagerLocationResolver:
    """Resolve one tenant location secret without exposing coordinates downstream."""

    def __init__(
        self, *, secret_prefix: str, region_name: str, client: object | None = None
    ) -> None:
        self.secret_prefix = secret_prefix.strip().strip("/")
        if not self.secret_prefix:
            raise ValueError("A location secret prefix is required")
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover
                raise RuntimeError(
                    "Install the platform extra: pip install -e '.[platform]'"
                ) from error
            client = boto3.client("secretsmanager", region_name=region_name)
        self.client = client

    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]:
        try:
            tenant = str(uuid.UUID(tenant_id))
            prefix = f"secret://tenant/{tenant}/locations/"
            if not location_ref.startswith(prefix):
                raise ValueError("reference prefix")
            location_id = str(uuid.UUID(location_ref.removeprefix(prefix)))
            secret_id = f"{self.secret_prefix}/{tenant}/locations/{location_id}"
            response = self.client.get_secret_value(SecretId=secret_id)
            raw = response.get("SecretString")
            if raw is None:
                raw = base64.b64decode(response["SecretBinary"]).decode("utf-8")
            payload = json.loads(raw)
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
            if not math.isfinite(latitude) or not -90 <= latitude <= 90:
                raise ValueError("latitude")
            if not math.isfinite(longitude) or not -180 <= longitude <= 180:
                raise ValueError("longitude")
            return {"latitude": latitude, "longitude": longitude}
        except Exception as error:
            raise VideoPipelineError(
                "location_unavailable",
                "Source location could not be resolved",
                retryable=True,
            ) from error


@dataclass(frozen=True)
class CameraConnection:
    transport: str
    input_url: str = field(repr=False)


def resolve_camera_connection(
    source: dict[str, Any], resolver: SecretResolver
) -> CameraConnection:
    connection = source["connection"]
    transport = connection["transport"]
    if transport not in {"hls", "rtsp", "onvif"}:
        raise VideoPipelineError(
            "camera_transport_invalid", "Live connector requires HLS, RTSP, or ONVIF"
        )
    endpoint = resolver.resolve_json(connection["endpoint_ref"])
    credential_ref = connection.get("credential_ref")
    if credential_ref is None and transport in {"rtsp", "onvif"}:
        raise VideoPipelineError(
            "camera_credentials_invalid",
            "RTSP and ONVIF camera credentials are required",
        )
    credential = resolver.resolve_json(credential_ref) if credential_ref else {}
    input_url = endpoint.get("stream_url") or endpoint.get("url")
    if not isinstance(input_url, str):
        raise VideoPipelineError(
            "camera_endpoint_invalid", "Camera endpoint secret is invalid"
        )
    parts = urlsplit(input_url)
    allowed_schemes = {
        "hls": {"https"},
        "rtsp": {"rtsp", "rtsps"},
        "onvif": {"rtsp", "rtsps"},
    }
    if (
        parts.scheme.lower() not in allowed_schemes[transport]
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise VideoPipelineError(
            "camera_endpoint_invalid", "Camera endpoint secret is invalid"
        )
    username, password = credential.get("username"), credential.get("password")
    if username is not None or password is not None:
        if not isinstance(username, str) or not isinstance(password, str):
            raise VideoPipelineError(
                "camera_credentials_invalid", "Camera credentials are invalid"
            )
        hostname = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        netloc = (
            f"{quote(username, safe='')}:{quote(password, safe='')}@{hostname}{port}"
        )
        input_url = urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
        )
    return CameraConnection(transport, input_url)


class FfmpegSegmenter:
    """Creates one bounded MP4 segment with transport-specific reconnect behavior."""

    def __init__(self, executable: str = "ffmpeg") -> None:
        self.executable = executable

    def capture(
        self, connection: CameraConnection, output: Path, *, duration_seconds: int
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        options = ["-nostdin", "-hide_banner", "-loglevel", "error"]
        if connection.input_url.startswith(("http://", "https://")):
            options += [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
            ]
        else:
            options += ["-rtsp_transport", "tcp", "-rw_timeout", "15000000"]
        command = [
            self.executable,
            *options,
            "-i",
            connection.input_url,
            "-t",
            str(duration_seconds),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=duration_seconds + 30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            output.unlink(missing_ok=True)
            raise VideoPipelineError(
                "camera_capture_unavailable", "Camera capture failed", retryable=True
            ) from error
        if result.returncode != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            raise VideoPipelineError(
                "camera_capture_failed",
                "Camera segment could not be created",
                retryable=True,
            )


class LiveCaptureWorker:
    """Applies backpressure and emits short live segments into the durable upload queue."""

    def __init__(
        self,
        *,
        store: Any,
        service: VideoPipelineService,
        broker: JobBroker,
        secrets: SecretResolver,
        telemetry: CoverageTelemetry,
        segmenter: FfmpegSegmenter,
        spool_root: Path,
        segment_seconds: int = 30,
        max_pending_segments: int = 20,
        max_reconnect_attempts: int = 5,
    ) -> None:
        self.store = store
        self.service = service
        self.broker = broker
        self.secrets = secrets
        self.telemetry = telemetry
        self.segmenter = segmenter
        self.spool_root = spool_root.resolve()
        self.spool_root.mkdir(parents=True, exist_ok=True)
        self.segment_seconds = segment_seconds
        self.max_pending_segments = max_pending_segments
        self.max_reconnect_attempts = max_reconnect_attempts

    def capture_once(self, tenant_id: str, source_id: str) -> dict[str, Any] | None:
        try:
            uuid.UUID(tenant_id)
            uuid.UUID(source_id)
        except (TypeError, ValueError) as error:
            raise VideoPipelineError(
                "camera_source_invalid",
                "Camera tenant and source identifiers must be UUIDs",
            ) from error
        started = datetime.now(UTC)
        if self.broker.depth() >= self.max_pending_segments:
            self._observe(
                tenant_id,
                source_id,
                started,
                connected=True,
                processable=False,
                frame_gap=True,
            )
            return None
        source = self.store.get_source(tenant_id, source_id)
        connection = resolve_camera_connection(source, self.secrets)
        segment = (
            self.spool_root / tenant_id / source_id / f"{started:%Y%m%dT%H%M%S%fZ}.mp4"
        )
        last_error: VideoPipelineError | None = None
        for attempt in range(self.max_reconnect_attempts):
            try:
                self.segmenter.capture(
                    connection, segment, duration_seconds=self.segment_seconds
                )
                break
            except VideoPipelineError as error:
                last_error = error
                if attempt + 1 < self.max_reconnect_attempts:
                    time.sleep(min(2**attempt, 30))
        else:
            self._observe(
                tenant_id,
                source_id,
                started,
                connected=False,
                processable=False,
                capture_failure=True,
            )
            raise last_error or VideoPipelineError(
                "camera_capture_failed", "Camera capture failed"
            )
        ended = started + timedelta(seconds=self.segment_seconds)
        try:
            asset = self.service.accept_upload(
                authenticated_tenant_id=tenant_id,
                source_id=source_id,
                path=segment,
                content_type="video/mp4",
                captured_start=_utc(started),
                captured_end=_utc(ended),
                duration_seconds=self.segment_seconds,
                consent_confirmed=True,
            )
            job = self.store.enqueue(tenant_id, asset["asset_id"], "upload")
            self.broker.publish(JobMessage(tenant_id, job["job_id"], "upload"))
            self._observe(
                tenant_id, source_id, started, connected=True, processable=True
            )
            return {
                "asset_id": asset["asset_id"],
                "job_id": job["job_id"],
                "status": job["state"],
            }
        finally:
            if not getattr(self.service.media_storage, "development_only", False):
                segment.unlink(missing_ok=True)

    def _observe(
        self,
        tenant_id: str,
        source_id: str,
        started: datetime,
        *,
        connected: bool,
        processable: bool,
        capture_failure: bool = False,
        frame_gap: bool = False,
    ) -> None:
        self.telemetry.record(
            CoverageObservation(
                tenant_id=tenant_id,
                source_id=source_id,
                observed_at=_utc(started),
                sample_seconds=self.segment_seconds,
                connected=connected,
                frame_processable=processable,
                detector_available=False,
                capture_failure=capture_failure,
                frame_gap=frame_gap,
            )
        )


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
