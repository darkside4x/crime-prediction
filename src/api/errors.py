"""Typed, schema-compatible API errors and request identifiers."""

from __future__ import annotations

from typing import Any
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.models.contracts import validate_contract
from src.models.errors import DataContractError
from src.data.video.errors import VideoPipelineError


def problem(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: list[dict[str, str]] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or [],
        },
    )


def _payload(request: Request, detail: Any, status_code: int) -> dict[str, Any]:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    if isinstance(detail, dict):
        code = str(detail.get("code", "request_failed"))
        message = str(detail.get("message", "The request could not be completed"))
        retryable = bool(detail.get("retryable", status_code >= 500))
        details = detail.get("details", [])
    else:
        code = "request_failed"
        message = str(detail)
        retryable = status_code >= 500
        details = []
    body = {
        "schema_version": "1.0.0",
        "request_id": request_id,
        "code": code,
        "message": message[:500],
        "retryable": retryable,
        "details": details,
    }
    validate_contract("api-error", body)
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        if not getattr(request.state, "request_id", None):
            request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content=_payload(request, error.detail, error.status_code),
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        details = []
        for item in error.errors()[:20]:
            location = ".".join(str(value) for value in item.get("loc", ())) or "request"
            details.append({"field": location[:128], "code": str(item.get("type", "invalid"))[:80]})
        return JSONResponse(
            status_code=422,
            content=_payload(
                request,
                {
                    "code": "request_validation_failed",
                    "message": "Request validation failed",
                    "retryable": False,
                    "details": details,
                },
                422,
            ),
        )

    @app.exception_handler(DataContractError)
    async def contract_error(request: Request, error: DataContractError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_payload(
                request,
                {
                    "code": "contract_validation_failed",
                    "message": str(error),
                    "retryable": False,
                    "details": [],
                },
                422,
            ),
        )

    @app.exception_handler(VideoPipelineError)
    async def video_pipeline_error(request: Request, error: VideoPipelineError) -> JSONResponse:
        status = {
            "resource_not_found": 404,
            "asset_not_found": 404,
            "candidate_expired": 409,
            "review_already_final": 409,
            "review_forbidden": 403,
            "reka_access_denied": 503,
            "reka_key_missing": 503,
        }.get(error.code, 503 if error.retryable else 422)
        return JSONResponse(
            status_code=status,
            content=_payload(
                request,
                {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "details": [],
                },
                status,
            ),
        )
