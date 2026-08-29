"""Replaceable authentication and server-derived tenant context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import threading
from typing import Protocol
import uuid

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

    development_only = True

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
        tenant_one_operator = TenantMembership(
            DEMO_TENANT_ONE, "demo-one", "Demo Tenant One", "platform_operator"
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
            os.environ.get("DEMO_OPERATOR_TOKEN", "demo-operator-one"): Principal(
                "principal-demo-operator", (tenant_one_operator,), DEMO_TENANT_ONE
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


class OidcAuthenticationProvider:
    """Validate asymmetric OIDC access tokens and derive tenant context from signed claims.

    The raw bearer token is never persisted. Active-tenant choices are keyed by a
    one-way token digest and may only select a membership present in the verified
    token. A deployment that needs choices to survive token rotation should inject
    an identity-provider session implementation instead of relying on this cache.
    """

    development_only = False

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: tuple[str, ...] = ("RS256",),
        memberships_claim: str = "tenant_memberships",
        leeway_seconds: int = 30,
        jwks_client: object | None = None,
    ) -> None:
        if not issuer.startswith("https://") or not jwks_url.startswith("https://"):
            raise ValueError("OIDC issuer and JWKS URL must use HTTPS")
        if not audience or not memberships_claim:
            raise ValueError("OIDC audience and memberships claim are required")
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if not algorithms or not set(algorithms) <= allowed:
            raise ValueError("Only allowlisted asymmetric JWT algorithms are supported")
        try:
            import jwt
        except ImportError as error:  # pragma: no cover - dependency guidance
            raise RuntimeError("Install the API extra to enable OIDC authentication") from error
        self._jwt = jwt
        self.issuer = issuer
        self.audience = audience
        self.algorithms = algorithms
        self.memberships_claim = memberships_claim
        self.leeway_seconds = leeway_seconds
        self.jwks_client = jwks_client or jwt.PyJWKClient(
            jwks_url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )
        self._active: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _decode(self, token: str) -> dict:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = self._jwt.decode(
                token,
                signing_key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except self._jwt.ExpiredSignatureError as error:
            raise HTTPException(
                status_code=401,
                detail={"code": "expired_token", "message": "Authentication token has expired"},
            ) from error
        except self._jwt.PyJWTError as error:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_token", "message": "Authentication token is invalid"},
            ) from error
        if not isinstance(claims, dict):
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_token", "message": "Authentication token is invalid"},
            )
        return claims

    def _principal(self, token: str, claims: dict) -> Principal:
        subject = claims.get("sub")
        raw_memberships = claims.get(self.memberships_claim)
        if not isinstance(subject, str) or not subject or not isinstance(raw_memberships, list):
            raise HTTPException(
                status_code=403,
                detail={"code": "tenant_membership_missing", "message": "No tenant membership is available"},
            )
        memberships: list[TenantMembership] = []
        seen: set[str] = set()
        for raw in raw_memberships:
            if not isinstance(raw, dict) or set(raw) != {"tenant_id", "slug", "display_name", "role"}:
                raise HTTPException(
                    status_code=401,
                    detail={"code": "invalid_token", "message": "Authentication token memberships are invalid"},
                )
            try:
                uuid.UUID(str(raw["tenant_id"]))
                membership = TenantMembership(
                    str(raw["tenant_id"]),
                    str(raw["slug"])[:80],
                    str(raw["display_name"])[:120],
                    str(raw["role"]),
                )
            except (TypeError, ValueError) as error:
                raise HTTPException(
                    status_code=401,
                    detail={"code": "invalid_token", "message": "Authentication token memberships are invalid"},
                ) from error
            if membership.tenant_id in seen:
                raise HTTPException(
                    status_code=401,
                    detail={"code": "invalid_token", "message": "Authentication token memberships are invalid"},
                )
            seen.add(membership.tenant_id)
            memberships.append(membership)
        if not memberships:
            raise HTTPException(
                status_code=403,
                detail={"code": "tenant_membership_missing", "message": "No tenant membership is available"},
            )
        token_key = self._token_key(token)
        requested = claims.get("active_tenant_id")
        with self._lock:
            active = self._active.get(token_key)
        if active not in seen:
            active = str(requested) if requested in seen else memberships[0].tenant_id
        return Principal(subject[:200], tuple(memberships), active)

    def authenticate(self, token: str) -> Principal:
        if not 1 <= len(token) <= 16384:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_token", "message": "Authentication token is invalid"},
            )
        return self._principal(token, self._decode(token))

    def switch_active_tenant(self, token: str, tenant_id: str) -> Principal:
        principal = self.authenticate(token)
        if tenant_id not in {item.tenant_id for item in principal.memberships}:
            raise HTTPException(
                status_code=403,
                detail={"code": "tenant_forbidden", "message": "Tenant membership is required"},
            )
        with self._lock:
            self._active[self._token_key(token)] = tenant_id
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
require_operator = require_roles("platform_operator")
