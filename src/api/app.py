"""Authenticated FastAPI boundary for the Phase 2 recorded-video slice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Literal
import uuid

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from src.models.contracts import validate_contract
from src.models.data import parse_utc
from src.models.operational import ForecastService

from . import demo_data, reka
from .errors import install_error_handlers, problem
from .settings import Settings
from .state import AuditLog, IdempotencyStore
from .tenancy import (
    AuthenticationProvider,
    DevelopmentAuthenticationProvider,
    TenantContext,
    context_for,
    require_owner,
    require_reviewer,
    require_tenant,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = [
    "Forecasts are aggregate area-level estimates with uncertainty, not ground truth.",
    "Historical incident reports may reflect reporting and enforcement patterns.",
    "Prohibited: individual assessment, suspect identification, facial recognition, or automated enforcement.",
    "Suppressed cells expose no numeric estimate and must not be interpreted as zero risk.",
    "Drivers are associations, not causes; human interpretation is required.",
]
CATEGORIES = ("property", "violence", "public_order", "traffic_safety")


class RecordedSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    registered_location_id: uuid.UUID
    retention_policy_days: int = Field(ge=1, le=30)


class LiveSourceCreate(RecordedSourceCreate):
    connection_secret_id: uuid.UUID
    transport: str = Field(pattern="^(hls|rtsp|onvif)$")


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed", "rejected"]
    confirmed_category: Literal[
        "property", "violence", "public_order", "traffic_safety", "other"
    ] | None = None
    rejection_reason: Literal[
        "false_positive", "insufficient_evidence", "duplicate", "outside_scope", "other"
    ] | None = None


class CopilotMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / "contracts" / "fixtures" / f"{name}.json").read_text())


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: source[key]
        for key in (
            "schema_version",
            "tenant_id",
            "source_id",
            "name",
            "mode",
            "status",
            "timezone",
            "retention_policy_days",
            "created_at",
            "updated_at",
        )
    }


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        west, south, east, north = (float(part) for part in value.split(","))
    except (ValueError, TypeError) as exc:
        raise problem(422, "invalid_bbox", "bbox must be west,south,east,north") from exc
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise problem(422, "invalid_bbox", "bbox coordinates are out of range or unordered")
    return west, south, east, north


def _future_row(
    tenant_id: str,
    cell_id: str,
    window_start: datetime,
    category: str,
    *,
    coverage_ratio: float,
) -> dict[str, Any]:
    seed = int(hashlib.sha256(f"{tenant_id}|{cell_id}|{category}".encode()).hexdigest()[:8], 16)
    lag_1, lag_2, lag_7, lag_14 = ((seed >> shift) % 4 for shift in (0, 3, 6, 9))
    data_as_of = min(datetime.now(timezone.utc), window_start - timedelta(seconds=1))
    hour_angle = 2 * math.pi * window_start.hour / 24
    day_angle = 2 * math.pi * window_start.weekday() / 7

    return {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "cell_id": cell_id,
        "interval_start": window_start.isoformat().replace("+00:00", "Z"),
        "category": category,
        "lag_1": lag_1,
        "lag_2": lag_2,
        "lag_7": lag_7,
        "lag_14": lag_14,
        "rolling_7_mean": (lag_1 + lag_2 + lag_7) / 3,
        "rolling_14_mean": (lag_1 + lag_2 + lag_7 + lag_14) / 4,
        "neighbor_lag_1": float((seed >> 12) % 5),
        "recent_trend": float(lag_1 - lag_7) / 3,
        "hour_sin": math.sin(hour_angle),
        "hour_cos": math.cos(hour_angle),
        "day_of_week_sin": math.sin(day_angle),
        "day_of_week_cos": math.cos(day_angle),
        "coverage_ratio": coverage_ratio,
        "data_as_of": data_as_of.isoformat().replace("+00:00", "Z"),
        "feature_snapshot_version": f"forecast-features-{window_start:%Y%m%dT%H%M%SZ}-v1",
    }


def _new_source(
    *, tenant_id: str, body: RecordedSourceCreate, mode: str, connection: dict[str, str]
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "source_id": str(uuid.uuid4()),
        "name": body.name,
        "mode": mode,
        "status": "draft",
        "timezone": body.timezone,
        "location_ref": f"secret://tenant/{tenant_id}/locations/{body.registered_location_id}",
        "connection": connection,
        "retention_policy_days": body.retention_policy_days,
        "created_at": now,
        "updated_at": now,
    }
    validate_contract("camera-source", source)
    return source


def create_app(
    provider: reka.RekaProvider | None = None,
    *,
    settings: Settings | None = None,
    auth_provider: AuthenticationProvider | None = None,
    forecast_service: ForecastService | None = None,
    coverage_provider: Callable[[str, str], float] | None = None,
) -> FastAPI:
    active_settings = settings or Settings.from_environment()
    if auth_provider is None and active_settings.app_environment == "production":
        raise ValueError("A production AuthenticationProvider must be injected")
    if provider is None:
        provider = (
            reka.RekaAPIProvider(
                api_key=active_settings.reka_api_key,
                base_url=active_settings.reka_chat_base_url,
                model=active_settings.reka_model,
                prompt_version=active_settings.reka_prompt_version,
                timeout_seconds=active_settings.reka_timeout_seconds,
            )
            if active_settings.reka_configured
            else reka.FakeRekaProvider()
        )

    app = FastAPI(
        title="Aggregate Incident Forecasting API",
        version="0.2.0",
        description="Tenant-isolated aggregate forecasts. No identity analysis or automated enforcement.",
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )
    app.state.settings = active_settings
    app.state.auth_provider = auth_provider or DevelopmentAuthenticationProvider()
    app.state.forecast_service = forecast_service or ForecastService()
    app.state.audit = AuditLog()
    app.state.idempotency = IdempotencyStore()
    app.state.forecasts = {}
    first_source = _load_fixture("camera-source")
    app.state.sources = {first_source["tenant_id"]: [first_source]}
    candidate = _load_fixture("candidate-detection")
    app.state.candidates = {candidate["tenant_id"]: {candidate["detection_id"]: candidate}}
    app.state.reviews = {}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        return {
            "status": "degraded",
            "reka_chat": "configured" if active_settings.reka_configured else "deterministic_fallback",
            "video_service": "not_connected",
            "forecast_models": "historical_fallback_only",
        }

    @app.get("/v1/me/tenants")
    def me_tenants(request: Request, ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        principal = request.state.principal
        return {
            "active_tenant_id": ctx.tenant_id,
            "tenants": [item.__dict__ for item in principal.memberships],
        }

    @app.put("/v1/me/active-tenant/{tenant_id}")
    def switch_tenant(
        tenant_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            principal = app.state.auth_provider.switch_active_tenant(
                request.state.bearer_token, tenant_id
            )
            switched = context_for(principal, ctx.request_id)
            app.state.audit.record(
                tenant_id=switched.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="active_tenant_changed",
                resource_type="tenant",
                resource_id=switched.tenant_id,
            )
            return {"active_tenant_id": switched.tenant_id, "role": switched.role}

        return app.state.idempotency.execute(
            tenant_id=ctx.principal_id,
            operation="switch_active_tenant",
            key=idempotency_key,
            payload={"tenant_id": tenant_id},
            action=action,
        )

    @app.get("/v1/metadata")
    def metadata(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        return {
            "categories": list(CATEGORIES),
            "h3_resolution": demo_data.H3_RESOLUTION,
            "forecast_window_minutes": 360,
            "limitations": LIMITATIONS,
        }

    @app.get("/v1/sources")
    def sources(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        return {"items": [_public_source(item) for item in app.state.sources.get(ctx.tenant_id, [])]}

    def create_source_record(
        body: RecordedSourceCreate,
        ctx: TenantContext,
        idempotency_key: str | None,
        mode: str,
        connection: dict[str, str],
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            source = _new_source(
                tenant_id=ctx.tenant_id, body=body, mode=mode, connection=connection
            )
            app.state.sources.setdefault(ctx.tenant_id, []).append(source)
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="source_registered",
                resource_type="camera_source",
                resource_id=source["source_id"],
            )
            return _public_source(source)

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"create_{mode}_source",
            key=idempotency_key,
            payload=body.model_dump(),
            action=action,
        )

    @app.post("/v1/sources/recorded-video", status_code=201)
    def create_recorded_source(
        body: RecordedSourceCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        return create_source_record(
            body, ctx, idempotency_key, "recorded_video", {"transport": "uploaded_asset"}
        )

    @app.post("/v1/sources/live-camera", status_code=201)
    def create_live_source(
        body: LiveSourceCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        return create_source_record(
            body,
            ctx,
            idempotency_key,
            "live_camera",
            {
                "transport": body.transport,
                "endpoint_ref": (
                    f"secret://tenant/{ctx.tenant_id}/connections/"
                    f"{body.connection_secret_id}/endpoint"
                ),
                "credential_ref": (
                    f"secret://tenant/{ctx.tenant_id}/connections/"
                    f"{body.connection_secret_id}/credentials"
                ),
            },
        )

    @app.post("/v1/video-assets/uploads", status_code=202)
    def create_video_upload(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        def unavailable() -> dict[str, Any]:
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="video_upload_requested",
                resource_type="video_asset",
                resource_id="unassigned",
                outcome="upstream_unavailable",
            )
            raise problem(
                503,
                "video_service_unavailable",
                "The Person 1 bounded media-intake service is not connected",
                retryable=True,
            )

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation="create_video_upload",
            key=idempotency_key,
            payload={"request_id": ctx.request_id},
            action=unavailable,
        )

    @app.get("/v1/ingestion/runs/{run_id}")
    def ingestion_run(
        run_id: uuid.UUID, ctx: TenantContext = Depends(require_tenant)
    ) -> dict[str, Any]:
        raise problem(404, "ingestion_run_not_found", "Ingestion run was not found")

    @app.get("/v1/candidate-detections")
    def candidates(
        limit: int = Query(default=50, ge=1, le=100),
        ctx: TenantContext = Depends(require_reviewer),
    ) -> dict[str, Any]:
        records = list(app.state.candidates.get(ctx.tenant_id, {}).values())[:limit]
        return {
            "items": [
                {
                    key: value
                    for key, value in record.items()
                    if key not in {"evidence_ref"}
                }
                | {"evidence_available": True}
                for record in records
            ]
        }

    @app.post("/v1/candidate-detections/{detection_id}/review", status_code=201)
    def review_candidate(
        detection_id: str,
        body: ReviewRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_reviewer),
    ) -> dict[str, Any]:
        candidate_record = app.state.candidates.get(ctx.tenant_id, {}).get(detection_id)
        if candidate_record is None:
            raise problem(404, "candidate_not_found", "Candidate detection was not found")
        if body.decision == "confirmed" and not body.confirmed_category:
            raise problem(422, "confirmed_category_required", "Confirmed reviews require a category")
        if body.decision == "rejected" and not body.rejection_reason:
            raise problem(422, "rejection_reason_required", "Rejected reviews require a reason")

        def action() -> dict[str, Any]:
            review_key = (ctx.tenant_id, detection_id)
            if review_key in app.state.reviews:
                raise problem(409, "review_final", "A final review already exists")
            result = {
                "schema_version": "1.0.0",
                "tenant_id": ctx.tenant_id,
                "review_id": str(uuid.uuid4()),
                "detection_id": detection_id,
                "decision": body.decision,
                "reviewed_by": ctx.principal_id,
                "reviewed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            if body.decision == "confirmed":
                result["confirmed_category"] = body.confirmed_category
                result["promoted_external_event_id"] = f"detection:{detection_id}"
            else:
                result["rejection_reason"] = body.rejection_reason
            validate_contract("candidate-review", result)
            app.state.reviews[review_key] = result
            candidate_record["review_status"] = body.decision
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action=f"candidate_{body.decision}",
                resource_type="candidate_detection",
                resource_id=detection_id,
            )
            return result

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"review_candidate:{detection_id}",
            key=idempotency_key,
            payload=body.model_dump(),
            action=action,
        )

    @app.get("/v1/coverage")
    def coverage(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        fixture = _load_fixture("coverage-snapshot")
        fixture["tenant_id"] = ctx.tenant_id
        return {"items": [fixture]}

    @app.get("/v1/forecasts")
    def forecasts(
        window_start: str = Query(...),
        category: str = Query(...),
        bbox: str | None = Query(default=None),
        page: int = Query(default=1, ge=1, le=10000),
        page_size: int = Query(default=50, ge=1, le=100),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        if category not in CATEGORIES:
            raise problem(422, "invalid_category", "Category is not allowlisted")
        start = parse_utc(window_start)
        now = datetime.now(timezone.utc)
        if start <= now:
            raise problem(422, "window_not_future", "Forecast window must start in the future")
        if start > now + timedelta(days=7):
            raise problem(422, "window_too_distant", "Forecast horizon is limited to seven days")
        bounds = _parse_bbox(bbox)
        cells = demo_data.tenant_cells(ctx.tenant_id)
        if bounds is not None:
            import h3

            west, south, east, north = bounds
            cells = [
                cell
                for cell in cells
                if (lambda point: south <= point[0] <= north and west <= point[1] <= east)(
                    h3.cell_to_latlng(cell)
                )
            ]
        total = len(cells)
        offset = (page - 1) * page_size
        selected = cells[offset : offset + page_size]
        measured_coverage = (
            coverage_provider(
                ctx.tenant_id,
                start.isoformat().replace("+00:00", "Z"),
            )
            if coverage_provider is not None
            else 0.0
        )
        items = [
            app.state.forecast_service.forecast(
                _future_row(
                    ctx.tenant_id,
                    cell,
                    start,
                    category,
                    coverage_ratio=measured_coverage,
                ),
                tenant_id=ctx.tenant_id,
                generated_at=now,
            )
            for cell in selected
        ]
        for item in items:
            app.state.forecasts[(ctx.tenant_id, item["forecast_id"])] = item
        return {"items": items, "page": page, "page_size": page_size, "total": total}

    @app.get("/v1/forecasts/{forecast_id}")
    def forecast_detail(
        forecast_id: str, ctx: TenantContext = Depends(require_tenant)
    ) -> dict[str, Any]:
        item = app.state.forecasts.get((ctx.tenant_id, forecast_id))
        if item is None:
            raise problem(404, "forecast_not_found", "Forecast was not found")
        return item

    @app.get("/v1/model-card")
    def model_card(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        card = _load_fixture("model-card")
        card["tenant_id"] = ctx.tenant_id
        return card

    @app.post("/v1/ai/copilot/messages")
    def copilot(
        body: CopilotMessage, ctx: TenantContext = Depends(require_tenant)
    ) -> dict[str, Any]:
        return reka.answer_question(ctx.tenant_id, body.question, provider)

    # Legacy routes remain read-only while Person 3 migrates to the frozen API.
    @app.get("/v1/risk", include_in_schema=False)
    def legacy_risk(
        window_start: str = Query(...),
        category: str = Query("all"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        valid_windows = {item["window_start"] for item in demo_data.windows()}
        if window_start not in valid_windows:
            raise problem(422, "invalid_window", "Window is not available")
        if category not in demo_data.CATEGORIES:
            raise problem(422, "invalid_category", "Category is not available")
        return demo_data.risk_feature_collection(ctx.tenant_id, window_start, category)

    @app.get("/v1/cells/{cell_id}/explanation", include_in_schema=False)
    def legacy_explanation(
        cell_id: str,
        window_start: str = Query(...),
        category: str = Query("all"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        if cell_id not in set(demo_data.tenant_cells(ctx.tenant_id)):
            raise problem(404, "cell_not_found", "Cell was not found")
        return {
            "prediction": demo_data.prediction_for(
                ctx.tenant_id, cell_id, window_start, category
            ),
            "recent_trend": demo_data.recent_trend(ctx.tenant_id, cell_id, category),
            "limitations": LIMITATIONS,
        }

    return app


app = create_app()
