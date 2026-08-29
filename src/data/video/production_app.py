"""Production FastAPI composition for horizontally scaled API replicas."""

from __future__ import annotations

import os
from pathlib import Path

from src.api.app import create_app
from src.api.forecasting import ForecastOrchestrator, PostgresForecastRepository
from src.api.settings import Settings
from src.api.state import PostgresAuditLog, PostgresIdempotencyStore
from src.data.platform_security import PostgresRateLimiter
from src.models.operational import ForecastService
from src.models.registry import FilesystemApprovedModelRegistry

from .capture import AwsSecretsManagerLocationResolver
from .coverage import StoreCoverageProvider
from .runtime import PlatformSettings, create_platform_runtime


def build_production_app():
    api_settings = Settings.from_environment()
    if api_settings.app_environment != "production":
        raise ValueError(
            "The production app factory requires APP_ENVIRONMENT=production"
        )
    platform_settings = PlatformSettings.from_environment()
    location_resolver = AwsSecretsManagerLocationResolver(
        secret_prefix=platform_settings.location_secret_prefix,
        region_name=platform_settings.aws_region,
    )
    runtime = create_platform_runtime(
        platform_settings,
        location_resolver=location_resolver,
    )
    model_root = Path(
        os.environ.get("MODEL_REGISTRY_ROOT", "/app/data/models")
    ).resolve()
    model_registry = FilesystemApprovedModelRegistry(model_root)
    forecast_service = ForecastService(models=model_registry)
    forecasts = ForecastOrchestrator(
        forecast_service,
        PostgresForecastRepository(runtime.database),
    )
    app = create_app(
        settings=api_settings,
        forecast_service=forecast_service,
        coverage_provider=StoreCoverageProvider(runtime.video_store),
        video_service=runtime.service,
        rate_limiter=PostgresRateLimiter(
            runtime.database,
            api_settings.api_rate_limit_requests,
            api_settings.api_rate_limit_window_seconds,
        ),
        forecast_orchestrator=forecasts,
        model_registry=model_registry,
        audit_log=PostgresAuditLog(runtime.database),
        idempotency_store=PostgresIdempotencyStore(runtime.database),
        video_broker=runtime.broker,
    )
    app.state.platform_runtime = runtime
    app.add_event_handler("shutdown", runtime.close)
    return app


app = build_production_app()
