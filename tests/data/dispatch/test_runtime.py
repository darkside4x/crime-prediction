from __future__ import annotations

import hashlib
import json

import pytest

from src.data.dispatch.runtime import DispatchSettings
from src.data.dispatch.twilio import TwilioMode


def _base_environment(monkeypatch: pytest.MonkeyPatch, *, live: bool = False) -> None:
    secret = {
        "callback_token_secret": "x" * 32,
        "configured": live,
    }
    if live:
        secret.update(
            {
                "account_sid": "AC-synthetic",
                "auth_token": "synthetic-token",
                "from_number_secret_ref": "secret://synthetic/from-number",
            }
        )
    values = {
        "DISPATCH_MODE": "live" if live else "mock",
        "DISPATCH_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/123456789012/dispatch",
        "DISPATCH_QUEUE_DLQ_URL": "https://sqs.ap-south-1.amazonaws.com/123456789012/dispatch-dlq",
        "DISPATCH_PUBLIC_BASE_URL": "https://api.example.test",
        "AWS_REGION": "ap-south-1",
        "DISPATCH_CONTACT_SECRET_PREFIX": "crime-prediction/test/tenants",
        "DISPATCH_TWILIO_SECRET": json.dumps(secret),
        "DISPATCH_EXTERNAL_CALLS_ENABLED": "false",
        "DISPATCH_TEST_CALLS_ENABLED": "false",
        "DISPATCH_APPROVED_DESTINATION_SHA256": "",
    }
    monkeypatch.delenv("DISPATCH_TWILIO_SECRET_FILE", raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_mock_mode_defaults_to_no_external_call_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch)

    settings = DispatchSettings.from_environment()

    assert settings.mode is TwilioMode.MOCK
    assert settings.external_calls_enabled is False
    assert settings.approved_destination_hashes == frozenset()
    assert "synthetic-token" not in repr(settings)


def test_mock_mode_rejects_external_call_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("DISPATCH_EXTERNAL_CALLS_ENABLED", "true")

    with pytest.raises(ValueError, match="cannot be enabled in mock mode"):
        DispatchSettings.from_environment()


def test_live_mode_requires_server_side_destination_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch, live=True)

    with pytest.raises(ValueError, match="approved destination hash"):
        DispatchSettings.from_environment()


def test_live_mode_accepts_only_hashed_approved_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch, live=True)
    approved = hashlib.sha256(b"+15555550101").hexdigest()
    monkeypatch.setenv("DISPATCH_APPROVED_DESTINATION_SHA256", approved)
    monkeypatch.setenv("DISPATCH_EXTERNAL_CALLS_ENABLED", "true")

    settings = DispatchSettings.from_environment()

    assert settings.mode is TwilioMode.LIVE
    assert settings.external_calls_enabled is True
    assert settings.approved_destination_hashes == frozenset({approved})


def test_boolean_safety_gates_reject_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("DISPATCH_EXTERNAL_CALLS_ENABLED", "yes")

    with pytest.raises(ValueError, match="must be true or false"):
        DispatchSettings.from_environment()
