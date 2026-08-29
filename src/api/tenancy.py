"""Replaceable authentication and server-derived tenant context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Protocol

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

DEMO_TENANT_ONE = "00000000-0000-4000-8000-000000000001"
DEMO_TENANT_TWO = "00000000-0000-4000-8000-000000000002"
ROLES = frozenset({"viewer", "reviewer", "tenant_admin", "platform_operator"})


@dataclass(frozen=True)
class TenantMembership:
    tenant_id: str
    slug: str
    display_name: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"Unsupported tenant role: {self.role}")


@dataclass(frozen=True)
class Principal:
    principal_id: str
    memberships: tuple[TenantMembership, ...]
    active_tenant_id: str


@dataclass(frozen=True)
class TenantContext:
    """Internal context created from verified claims, never request payloads."""

    request_id: str
    principal_id: str
    tenant_id: str
    slug: str
    display_name: str
    role: str
    authenticated_at: str


class AuthenticationProvider(Protocol):
    def authenticate(self, token: str) -> Principal: ...

    def switch_active_tenant(self, token: str, tenant_id: str) -> Principal: ...


class DevelopmentAuthenticationProvider:
    """Deterministic development provider with explicit tenant memberships."""

    def __init__(self) -> None:
        tenant_one_admin = TenantMembership(
            DEMO_TENANT_ONE, "demo-one", "Demo Tenant One", "tenant_admin"
        )
        tenant_one_reviewer = TenantMembership(
            DEMO_TENANT_ONE, "demo-one", "Demo Tenant One", "reviewer"
        )
        tenant_one_viewer = TenantMembership(
            DEMO_TENANT_ONE, "demo-one", "Demo Tenant One", "viewer"
        )
        tenant_two_viewer = TenantMembership(
            DEMO_TENANT_TWO, "demo-two", "Demo Tenant Two", "viewer"
        )
        self._principals = {
            os.environ.get("DEMO_TOKEN_ONE", "demo-token-one"): Principal(
                "principal-demo-admin",
                (tenant_one_admin, tenant_two_viewer),
                DEMO_TENANT_ONE,
            ),
            os.environ.get("DEMO_TOKEN_TWO", "demo-token-two"): Principal(
                "principal-demo-viewer", (tenant_two_viewer,), DEMO_TENANT_TWO
            ),
            os.environ.get("DEMO_REVIEWER_TOKEN", "demo-reviewer-one"): Principal(
                "principal-demo-reviewer", (tenant_one_reviewer,), DEMO_TENANT_ONE
            ),
            os.environ.get("DEMO_VIEWER_TOKEN", "demo-viewer-one"): Principal(
                "principal-demo-viewer-one", (tenant_one_viewer,), DEMO_TENANT_ONE
            ),
        }
        self._active = {
            token: principal.active_tenant_id for token, principal in self._principals.items()
        }

    def authenticate(self, token: str) -> Principal:
        if token == "expired-demo-token":
            raise HTTPException(
                status_code=401,
                detail={"code": "expired_token", "message": "Authentication token has expired"},
            )
        principal = self._principals.get(token)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_token", "message": "Authentication token is invalid"},
            )
        return Principal(
            principal.principal_id,
            principal.memberships,
            self._active[token],
        )

    def switch_active_tenant(self, token: str, tenant_id: str) -> Principal:
        principal = self.authenticate(token)
        if tenant_id not in {item.tenant_id for item in principal.memberships}:
            raise HTTPException(
                status_code=403,
                detail={"code": "tenant_forbidden", "message": "Tenant membership is required"},
            )
        self._active[token] = tenant_id
        return self.authenticate(token)


_bearer = HTTPBearer(auto_error=False)


def context_for(principal: Principal, request_id: str) -> TenantContext:
    membership = next(
        item for item in principal.memberships if item.tenant_id == principal.active_tenant_id
    )
    return TenantContext(
        request_id=request_id,
        principal_id=principal.principal_id,
        tenant_id=membership.tenant_id,
        slug=membership.slug,
        display_name=membership.display_name,
        role=membership.role,
        authenticated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


async def require_tenant(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> TenantContext:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "missing_token", "message": "Bearer authentication is required"},
        )
    if "tenant_id" in request.query_params:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "client_tenant_forbidden",
                "message": "tenant_id is resolved from authentication, not query parameters",
            },
        )
    provider: AuthenticationProvider = request.app.state.auth_provider
    principal = provider.authenticate(credentials.credentials)
    request.state.principal = principal
    request.state.bearer_token = credentials.credentials
    return context_for(principal, request.state.request_id)


def require_roles(*allowed: str):
    unsupported = set(allowed).difference(ROLES)
    if unsupported:
        raise ValueError(f"Unsupported roles: {sorted(unsupported)}")

    async def dependency(context: TenantContext = Depends(require_tenant)) -> TenantContext:
        if context.role not in allowed:
            raise HTTPException(
                status_code=403,
                detail={"code": "role_forbidden", "message": "Role is not permitted"},
            )
        return context

    return dependency


require_owner = require_roles("tenant_admin", "platform_operator")
require_reviewer = require_roles("reviewer", "tenant_admin", "platform_operator")
