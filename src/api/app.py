"""FastAPI application implementing the docs/ARCHITECTURE.md §9 surface."""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import demo_data, reka
from .tenancy import TenantContext, require_owner, require_tenant

LIMITATIONS = [
    "Forecasts are aggregate area-level estimates with uncertainty, not ground truth.",
    "Historical incident reports may reflect reporting and enforcement patterns.",
    "Prohibited: individual-level assessment, suspect identification, automated enforcement.",
    "Low-support cells and windows are suppressed and show no numeric value.",
    "A human must interpret this output; drivers are associations, not causes.",
]


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern="^(recorded_replay|webhook|kafka|mqtt)$")
    secret_ref: str = Field(min_length=1, max_length=200)


class CopilotMessage(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def create_app() -> FastAPI:
    app = FastAPI(title="Crime Hotspot Forecasting API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(","),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    provider: reka.RekaProvider = reka.FakeRekaProvider()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me/tenants")
    def me_tenants(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        return {"tenants": [{
            "tenant_id": ctx.tenant_id, "slug": ctx.slug,
            "display_name": ctx.display_name, "role": ctx.role,
        }]}

    @app.get("/v1/metadata")
    def metadata(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        return {
            "categories": demo_data.CATEGORIES,
            "windows": demo_data.windows(),
            "h3_resolution": demo_data.H3_RESOLUTION,
            "model_version": demo_data.MODEL_VERSION,
            "data_version": demo_data.DATA_VERSION,
            "data_as_of": demo_data.DATA_AS_OF,
            "limitations": LIMITATIONS,
        }

    @app.get("/v1/sources")
    def sources(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        return {"sources": demo_data.sources_for(ctx.tenant_id)}

    @app.post("/v1/sources", status_code=201)
    def create_source(body: SourceCreate, ctx: TenantContext = Depends(require_owner)) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "tenant_id": ctx.tenant_id,
            "source_id": str(uuid.uuid4()),
            "name": body.name, "kind": body.kind, "status": "draft",
            "audit": {"actor_role": ctx.role, "action": "source_created"},
        }

    @app.post("/v1/sources/{source_id}/replays", status_code=202)
    def start_replay(source_id: str, ctx: TenantContext = Depends(require_owner)) -> dict[str, Any]:
        owned = {s["source_id"] for s in demo_data.sources_for(ctx.tenant_id)}
        if source_id not in owned:
            raise HTTPException(status_code=404, detail={"code": "source_not_found"})
        return {"run_id": str(uuid.uuid4()), "status": "running", "source_id": source_id}

    @app.get("/v1/risk")
    def risk(
        window_start: str = Query(...),
        category: str = Query("all"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        valid_windows = {w["window_start"] for w in demo_data.windows()}
        if window_start not in valid_windows:
            raise HTTPException(status_code=422, detail={"code": "invalid_window",
                                                         "valid": sorted(valid_windows)})
        if category not in demo_data.CATEGORIES:
            raise HTTPException(status_code=422, detail={"code": "invalid_category"})
        return demo_data.risk_feature_collection(ctx.tenant_id, window_start, category)

    @app.get("/v1/cells/{cell_id}/explanation")
    def explanation(
        cell_id: str,
        window_start: str = Query(...),
        category: str = Query("all"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        if cell_id not in set(demo_data.tenant_cells(ctx.tenant_id)):
            raise HTTPException(status_code=404, detail={"code": "cell_not_found"})
        pred = demo_data.prediction_for(ctx.tenant_id, cell_id, window_start, category)
        return {
            "prediction": pred,
            "recent_trend": demo_data.recent_trend(ctx.tenant_id, cell_id, category),
            "limitations": LIMITATIONS,
        }

    @app.get("/v1/model-card")
    def model_card(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        import json

        card = json.loads((reka.REPO_ROOT / "contracts" / "fixtures" / "model-card.json").read_text())
        card["tenant_id"] = ctx.tenant_id
        return card

    @app.post("/v1/ai/copilot/messages")
    def copilot(body: CopilotMessage, ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        return reka.answer_question(ctx.tenant_id, body.question, provider)

    return app


app = create_app()
