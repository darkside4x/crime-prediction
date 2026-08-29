from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import pytest

from src.api import reka
from src.api.app import create_app
from src.api.security import InMemoryRateLimiter
from src.api.settings import Settings
from src.api.tenancy import OidcAuthenticationProvider


TENANT = "00000000-0000-4000-8000-000000000001"


class _SigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _JwksClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str):
        return _SigningKey(self.key)


def _provider() -> tuple[OidcAuthenticationProvider, object]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = OidcAuthenticationProvider(
        issuer="https://identity.example/",
        audience="crime-api",
        jwks_url="https://identity.example/jwks.json",
        jwks_client=_JwksClient(private.public_key()),
    )
    return provider, private


def _token(private, **updates) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": "user-123",
        "iss": "https://identity.example/",
        "aud": "crime-api",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "tenant_memberships": [
            {
                "tenant_id": TENANT,
                "slug": "tenant-one",
                "display_name": "Tenant One",
                "role": "tenant_admin",
            }
        ],
    }
    claims.update(updates)
    return jwt.encode(claims, private, algorithm="RS256")


def test_oidc_validates_signature_issuer_audience_expiry_and_memberships() -> None:
    provider, private = _provider()
    token = _token(private)
    principal = provider.authenticate(token)
    assert principal.principal_id == "user-123"
    assert principal.active_tenant_id == TENANT

    expired = _token(private, exp=datetime.now(timezone.utc) - timedelta(minutes=2))
    with pytest.raises(Exception) as error:
        provider.authenticate(expired)
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "expired_token"

    invalid_audience = _token(private, aud="some-other-api")
    with pytest.raises(Exception) as error:
        provider.authenticate(invalid_audience)
    assert error.value.detail["code"] == "invalid_token"


def test_oidc_active_tenant_switch_cannot_escape_verified_memberships() -> None:
    provider, private = _provider()
    token = _token(private)
    with pytest.raises(Exception) as error:
        provider.switch_active_tenant(token, "00000000-0000-4000-8000-000000000002")
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "tenant_forbidden"


def test_security_middleware_bounds_requests_rate_and_sets_headers() -> None:
    settings = Settings(
        api_rate_limit_requests=2,
        api_rate_limit_window_seconds=60,
        max_request_bytes=1024,
    )
    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=settings,
            rate_limiter=InMemoryRateLimiter(2, 60),
        )
    )
    headers = {"Authorization": "Bearer demo-viewer-one"}
    first = client.get("/v1/metadata", headers=headers)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert client.get("/v1/metadata", headers=headers).status_code == 200
    limited = client.get("/v1/metadata", headers=headers)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limit_exceeded"

    separate = TestClient(create_app(provider=reka.FakeRekaProvider(), settings=settings))
    too_large = separate.post(
        "/v1/ai/copilot/messages",
        headers={**headers, "Content-Length": "2048"},
        content=b"{}",
    )
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "request_too_large"


def test_mutation_without_content_length_is_rejected() -> None:
    app = create_app(provider=reka.FakeRekaProvider(), settings=Settings())
    transport = httpx.ASGITransport(app=app)

    async def body():
        yield b"{}"

    import asyncio

    async def request():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/v1/ai/copilot/messages",
                headers={"Authorization": "Bearer demo-viewer-one"},
                content=body(),
            )

    response = asyncio.run(request())
    assert response.status_code == 411
    assert response.json()["code"] == "content_length_required"


def test_production_rejects_development_security_services() -> None:
    with pytest.raises(ValueError, match="production AuthenticationProvider"):
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(app_environment="production"),
        )
