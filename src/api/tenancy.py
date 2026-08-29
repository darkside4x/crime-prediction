"""Authentication and tenant-context middleware.

Tenant identity is resolved server-side from the bearer token.
It is never accepted as a client-controlled filter (AGENTS.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

DEMO_TENANT_ONE = "00000000-0000-4000-8000-000000000001"
DEMO_TENANT_TWO = "00000000-0000-4000-8000-000000000002"


@dataclass(frozen=True)
class TenantContext:
    """Non-optional tenant context passed through every service call."""

    tenant_id: str
    slug: str
    display_name: str
    role: str


def _demo_token_map() -> dict[str, TenantContext]:
    """Static demo principals. Real deployments swap in an IdP verifier."""
    return {
        os.environ.get("DEMO_TOKEN_ONE", "demo-token-one"): TenantContext(
            tenant_id=DEMO_TENANT_ONE,
            slug="demo-one",
            display_name="Demo Tenant One",
            role="admin",
        ),
        os.environ.get("DEMO_TOKEN_TWO", "demo-token-two"): TenantContext(
            tenant_id=DEMO_TENANT_TWO,
            slug="demo-two",
            display_name="Demo Tenant Two",
            role="analyst",
        ),
    }


_bearer = HTTPBearer(auto_error=False)


async def require_tenant(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> TenantContext:
    if credentials is None:
        raise HTTPException(status_code=401, detail={"code": "missing_token"})
    context = _demo_token_map().get(credentials.credentials)
    if context is None:
        raise HTTPException(status_code=401, detail={"code": "invalid_token"})
    # Reject any client attempt to smuggle a tenant filter.
    if "tenant_id" in request.query_params:
        raise HTTPException(
            status_code=400,
            detail={"code": "client_tenant_forbidden",
                    "message": "tenant_id is resolved from authentication, not query parameters"},
        )
    return context


async def require_owner(context: TenantContext = Depends(require_tenant)) -> TenantContext:
    if context.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail={"code": "role_forbidden"})
    return context
