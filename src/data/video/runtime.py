"""Production Person 1 runtime composition."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.data.postgres import PostgresIngestionStore, TenantPostgres

from .broker import SqsJobBroker
from .postgres import PostgresVideoStore
from .reka import RekaVisionProvider
from .service import LocationResolver, VideoPipelineService
from .storage import ClamAVCommandScanner, S3MediaStorage


@dataclass(frozen=True)
class PlatformSettings:
    database_url: str
    queue_url: str
    queue_dlq_url: str
    media_bucket: str
    media_kms_key_id: str
    aws_region: str
    reka_api_key: str
    reka_vision_base_url: str
    worker_lease_seconds: int
    restricted_spool_root: Path

    @classmethod
    def from_environment(cls) -> PlatformSettings:
        required = {
            "database_url": "DATABASE_URL",
            "queue_url": "VIDEO_QUEUE_URL",
            "queue_dlq_url": "VIDEO_QUEUE_DLQ_URL",
            "media_bucket": "VIDEO_MEDIA_BUCKET",
            "media_kms_key_id": "VIDEO_MEDIA_KMS_KEY_ID",
            "aws_region": "AWS_REGION",
            "reka_api_key": "REKA_API_KEY",
        }
        values: dict[str, Any] = {}
        missing: list[str] = []
        for field, variable in required.items():
            value = os.getenv(variable, "").strip()
            if not value or value.startswith("replace-"):
                missing.append(variable)
            values[field] = value
        if missing:
            raise ValueError(f"Missing production platform settings: {', '.join(sorted(missing))}")
        values.update(
            reka_vision_base_url=os.getenv(
                "REKA_VISION_BASE_URL", "https://vision-agent.api.reka.ai"
            ),
            worker_lease_seconds=int(os.getenv("VIDEO_WORKER_LEASE_SECONDS", "120")),
            restricted_spool_root=Path(os.getenv("VIDEO_SPOOL_ROOT", "/var/lib/crime-video-spool")),
        )
        return cls(**values)


@dataclass
class PlatformRuntime:
    database: TenantPostgres
    ingestion_store: PostgresIngestionStore
    video_store: PostgresVideoStore
    media_storage: S3MediaStorage
    broker: SqsJobBroker
    service: VideoPipelineService

    def close(self) -> None:
        self.database.close()


def create_platform_runtime(
    settings: PlatformSettings, *, location_resolver: LocationResolver
) -> PlatformRuntime:
    database = TenantPostgres(settings.database_url)
    ingestion_store = PostgresIngestionStore(database)
    video_store = PostgresVideoStore(database, ingestion_store)
    media_storage = S3MediaStorage(
        bucket=settings.media_bucket,
        kms_key_id=settings.media_kms_key_id,
        region_name=settings.aws_region,
    )
    broker = SqsJobBroker(
        queue_url=settings.queue_url,
        dead_letter_queue_url=settings.queue_dlq_url,
        region_name=settings.aws_region,
        visibility_seconds=settings.worker_lease_seconds,
    )
    provider = RekaVisionProvider(
        settings.reka_api_key, base_url=settings.reka_vision_base_url
    )
    service = VideoPipelineService(
        video_store,
        provider,
        location_resolver,
        media_root=settings.restricted_spool_root,
        media_storage=media_storage,
        media_scanner=ClamAVCommandScanner(),
    )
    return PlatformRuntime(
        database, ingestion_store, video_store, media_storage, broker, service
    )
