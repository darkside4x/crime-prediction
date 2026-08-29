"""Transport-level request bounds, rate limiting, and security headers."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import threading
import time
from typing import Protocol
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.models.contracts import validate_contract


class RateLimiter(Protocol):
    development_only: bool

    def allow(self, key: str, *, now: float | None = None) -> tuple[bool, int]: ...


class InMemoryRateLimiter:
    """Bounded fixed-process limiter for development and single-process tests."""

    development_only = True

    def __init__(self, requests: int, window_seconds: int) -> None:
        if requests < 1 or window_seconds < 1:
            raise ValueError("Rate limit and window must be positive")
        self.requests = requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> tuple[bool, int]:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                retry_after = max(1, int(self.window_seconds - (current - events[0])))
                return False, retry_after
            events.append(current)
            if not events:
                self._events.pop(key, None)
            return True, 0


def _rate_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:]
        return "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    client = request.client.host if request.client else "unknown"
    return "client:" + hashlib.sha256(client.encode("utf-8")).hexdigest()


def _error(request_id: str, status: int, code: str, message: str, *, retryable: bool) -> JSONResponse:
    body = {
        "schema_version": "1.0.0",
        "request_id": request_id,
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": [],
    }
    validate_contract("api-error", body)
    return JSONResponse(status_code=status, content=body, headers={"X-Request-ID": request_id})


class ApiSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        max_request_bytes: int,
        rate_limiter: RateLimiter,
        production: bool,
    ) -> None:
        super().__init__(app)
        self.max_request_bytes = max_request_bytes
        self.rate_limiter = rate_limiter
        self.production = production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
        request.state.request_id = request_id
        raw_length = request.headers.get("content-length")
        if request.method in {"POST", "PUT", "PATCH"} and raw_length is None:
            return _error(
                request_id,
                411,
                "content_length_required",
                "Content-Length is required for mutation requests",
                retryable=False,
            )
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                return _error(request_id, 400, "content_length_invalid", "Content-Length is invalid", retryable=False)
            if content_length < 0 or content_length > self.max_request_bytes:
                return _error(request_id, 413, "request_too_large", "Request exceeds the configured size limit", retryable=False)
        allowed, retry_after = self.rate_limiter.allow(_rate_key(request))
        if not allowed:
            response = _error(request_id, 429, "rate_limit_exceeded", "Request rate limit exceeded", retryable=True)
            response.headers["Retry-After"] = str(retry_after)
            return response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        if self.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
