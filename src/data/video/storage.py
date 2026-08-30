"""Restricted media storage and malware-scanning adapters."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .errors import VideoPipelineError


class MediaScanner(Protocol):
    def scan(self, path: Path) -> None: ...


class MediaStorage(Protocol):
    def store(
        self, path: Path, *, tenant_id: str, asset_id: str, sha256: str
    ) -> str: ...
    @contextmanager
    def materialize(
        self, storage_ref: str, *, tenant_id: str, asset_id: str
    ) -> Iterator[Path]: ...
    def delete(self, storage_ref: str, *, tenant_id: str, asset_id: str) -> None: ...


class NoOpMediaScanner:
    """Explicit development/test scanner; production runtime rejects this adapter."""

    development_only = True

    def scan(self, path: Path) -> None:
        if not path.is_file():
            raise VideoPipelineError("video_path_invalid", "Video file was not found")


class ClamAVCommandScanner:
    """Bounded ClamAV scanner with no media contents copied into errors or logs."""

    development_only = False

    def __init__(
        self, executable: str = "clamscan", *, timeout_seconds: float = 120
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def scan(self, path: Path) -> None:
        try:
            result = subprocess.run(
                [self.executable, "--no-summary", "--infected", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VideoPipelineError(
                "malware_scanner_unavailable",
                "Media malware scanner was unavailable",
                retryable=True,
            ) from error
        if result.returncode == 1:
            raise VideoPipelineError(
                "malware_detected", "Uploaded media failed malware scanning"
            )
        if result.returncode != 0:
            raise VideoPipelineError(
                "malware_scan_failed",
                "Uploaded media could not be scanned",
                retryable=True,
            )


class LocalMediaStorage:
    """Restricted-root adapter for local development; paths never enter public payloads."""

    development_only = True

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def store(self, path: Path, *, tenant_id: str, asset_id: str, sha256: str) -> str:
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(self.root):
            raise VideoPipelineError(
                "video_path_invalid", "Video escaped the restricted media root"
            )
        return f"file://{resolved}"

    @contextmanager
    def materialize(
        self, storage_ref: str, *, tenant_id: str, asset_id: str
    ) -> Iterator[Path]:
        if not storage_ref.startswith("file://"):
            raise VideoPipelineError(
                "storage_ref_invalid", "Local media reference is invalid"
            )
        path = Path(storage_ref[7:]).resolve()
        if not path.is_file() or not path.is_relative_to(self.root):
            raise VideoPipelineError(
                "storage_ref_invalid", "Local media reference escaped its root"
            )
        yield path

    def delete(self, storage_ref: str, *, tenant_id: str, asset_id: str) -> None:
        with self.materialize(
            storage_ref, tenant_id=tenant_id, asset_id=asset_id
        ) as path:
            path.unlink(missing_ok=True)


class S3MediaStorage:
    """Tenant-prefixed private S3 storage with mandatory KMS encryption."""

    development_only = False
    reference_prefix = "secret://s3-media/"

    def __init__(
        self,
        *,
        bucket: str,
        kms_key_id: str,
        expected_bucket_owner: str | None = None,
        region_name: str,
        client: object | None = None,
    ) -> None:
        if not bucket or not kms_key_id:
            raise ValueError("S3 bucket and KMS key are required")
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover
                raise RuntimeError(
                    "Install the platform extra: pip install -e '.[platform]'"
                ) from error
            client = boto3.client("s3", region_name=region_name)
        self.client = client
        self.bucket = bucket
        self.kms_key_id = kms_key_id
        if expected_bucket_owner is not None and not re.fullmatch(
            r"[0-9]{12}", expected_bucket_owner
        ):
            raise ValueError(
                "Expected S3 bucket owner must be a 12-digit AWS account ID"
            )
        self.expected_bucket_owner = expected_bucket_owner

    def _owner_args(self) -> dict[str, str]:
        return (
            {"ExpectedBucketOwner": self.expected_bucket_owner}
            if self.expected_bucket_owner is not None
            else {}
        )

    @staticmethod
    def _key(tenant_id: str, asset_id: str) -> str:
        if "/" in tenant_id or "/" in asset_id or ".." in tenant_id or ".." in asset_id:
            raise ValueError(
                "Tenant and asset identifiers cannot contain path separators"
            )
        return f"tenants/{tenant_id}/video-assets/{asset_id}/original.mp4"

    def store(self, path: Path, *, tenant_id: str, asset_id: str, sha256: str) -> str:
        key = self._key(tenant_id, asset_id)
        try:
            self.client.upload_file(
                str(path),
                self.bucket,
                key,
                ExtraArgs={
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": self.kms_key_id,
                    "ContentType": "video/mp4",
                    "Metadata": {"sha256": sha256},
                    **self._owner_args(),
                },
            )
        except Exception as error:
            raise VideoPipelineError(
                "media_storage_unavailable",
                "Encrypted media storage was unavailable",
                retryable=True,
            ) from error
        return f"{self.reference_prefix}{tenant_id}/{asset_id}"

    def _validate_ref(self, storage_ref: str, tenant_id: str, asset_id: str) -> str:
        expected = f"{self.reference_prefix}{tenant_id}/{asset_id}"
        if storage_ref != expected:
            raise VideoPipelineError(
                "storage_ref_invalid", "Media reference did not match tenant asset"
            )
        return self._key(tenant_id, asset_id)

    @contextmanager
    def materialize(
        self, storage_ref: str, *, tenant_id: str, asset_id: str
    ) -> Iterator[Path]:
        key = self._validate_ref(storage_ref, tenant_id, asset_id)
        directory = Path(tempfile.mkdtemp(prefix="crime-video-worker-"))
        target = directory / "input.mp4"
        try:
            head = self.client.head_object(
                Bucket=self.bucket,
                Key=key,
                **self._owner_args(),
            )
            expected_sha256 = str(head.get("Metadata", {}).get("sha256", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise VideoPipelineError(
                    "media_integrity_invalid",
                    "Encrypted media checksum metadata was invalid",
                )
            self.client.download_file(
                self.bucket,
                key,
                str(target),
                ExtraArgs=self._owner_args() or None,
            )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != expected_sha256:
                raise VideoPipelineError(
                    "media_integrity_mismatch",
                    "Encrypted media failed checksum verification",
                )
            yield target
        except VideoPipelineError:
            raise
        except Exception as error:
            raise VideoPipelineError(
                "media_materialization_failed",
                "Encrypted media could not be materialized",
                retryable=True,
            ) from error
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def delete(self, storage_ref: str, *, tenant_id: str, asset_id: str) -> None:
        key = self._validate_ref(storage_ref, tenant_id, asset_id)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key, **self._owner_args())
        except Exception as error:
            raise VideoPipelineError(
                "media_delete_failed", "Encrypted media deletion failed", retryable=True
            ) from error
