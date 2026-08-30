from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring

import pytest

from src.data.dispatch import (
    CallScript,
    ContactRole,
    DispatchConfigurationError,
    InvalidWebhookSignature,
    OutboundCallRequest,
    ResponseContact,
    TwilioSdkSignatureVerifier,
    TwilioSdkVoiceProvider,
    VoiceSubmissionUncertain,
    render_voice_twiml,
)

APPROVED_DESTINATIONS = frozenset({hashlib.sha256(b"+15555550101").hexdigest()})


class SecretResolver:
    def __init__(self) -> None:
        self.values = {
            "secret://dispatch/to": "+15555550101",
            "secret://dispatch/from": "+15555550102",
        }

    def resolve(self, reference: str) -> str:
        return self.values[reference]


class FakeCalls:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(sid="CA-test-call-001")

    def __call__(self, reference: str):
        calls = self

        class CallResource:
            def update(self, **kwargs):
                calls.updated.append((reference, kwargs))

        return CallResource()


class FakeClient:
    def __init__(self) -> None:
        self.calls = FakeCalls()


class RecordingValidator:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.received = None

    def validate(self, url, params, signature):
        self.received = (url, params, signature)
        return self.result


def _script() -> CallScript:
    return CallScript(
        case_reference="CH-1042",
        category="traffic_safety",
        broad_location_label="Demo Zone A",
        occurred_at=datetime(2026, 8, 30, 12, 30, tzinfo=UTC),
    )


def _request() -> OutboundCallRequest:
    return OutboundCallRequest(
        request_id="attempt-0001",
        destination_secret_ref="secret://dispatch/to",
        callback_token="a" * 43,
        attempt_number=1,
        target_role=ContactRole.PRIMARY,
        script=_script(),
        ring_timeout_seconds=20,
    )


def test_live_twilio_adapter_is_disabled_by_default() -> None:
    client = FakeClient()
    provider = TwilioSdkVoiceProvider(
        client=client,
        secret_resolver=SecretResolver(),
        from_number_secret_ref="secret://dispatch/from",
        public_base_url="https://api.example.test",
    )

    with pytest.raises(DispatchConfigurationError) as caught:
        provider.place_call(_request())

    assert caught.value.code == "twilio_live_calls_disabled"
    assert client.calls.created == []


def test_sdk_adapter_uses_bounded_recording_free_callbacks() -> None:
    client = FakeClient()
    provider = TwilioSdkVoiceProvider(
        client=client,
        secret_resolver=SecretResolver(),
        from_number_secret_ref="secret://dispatch/from",
        public_base_url="https://api.example.test",
        enabled=True,
        approved_destination_hashes=APPROVED_DESTINATIONS,
    )

    result = provider.place_call(_request())
    provider.cancel_call(result.provider_call_reference)

    assert result.provider_call_reference == "CA-test-call-001"
    sent = client.calls.created[0]
    assert sent["url"].endswith(f"/v1/twilio/voice/{'a' * 43}")
    assert sent["status_callback"].endswith(f"/v1/twilio/status/{'a' * 43}")
    assert sent["async_amd_status_callback"].endswith(f"/v1/twilio/amd/{'a' * 43}")
    assert sent["timeout"] == 20
    assert sent["record"] is False
    assert sent["machine_detection"] == "Enable"
    assert "body" not in sent
    assert client.calls.updated == [("CA-test-call-001", {"status": "completed"})]


def test_sdk_provider_failure_suppresses_sensitive_provider_exception_causes() -> None:
    class FailingCalls(FakeCalls):
        def create(self, **kwargs):
            del kwargs
            raise RuntimeError("destination +15555550101 secret://dispatch/to")

    client = FakeClient()
    client.calls = FailingCalls()
    provider = TwilioSdkVoiceProvider(
        client=client,
        secret_resolver=SecretResolver(),
        from_number_secret_ref="secret://dispatch/from",
        public_base_url="https://api.example.test",
        enabled=True,
        approved_destination_hashes=APPROVED_DESTINATIONS,
    )

    with pytest.raises(VoiceSubmissionUncertain) as caught:
        provider.place_call(_request())

    assert caught.value.__cause__ is None
    assert "+15555550101" not in str(caught.value)
    assert "dispatch/to" not in repr(caught.value)


def test_sdk_missing_call_reference_is_an_uncertain_submission() -> None:
    class MissingReferenceCalls(FakeCalls):
        def create(self, **kwargs):
            self.created.append(kwargs)
            return SimpleNamespace(sid="")

    client = FakeClient()
    client.calls = MissingReferenceCalls()
    provider = TwilioSdkVoiceProvider(
        client=client,
        secret_resolver=SecretResolver(),
        from_number_secret_ref="secret://dispatch/from",
        public_base_url="https://api.example.test",
        enabled=True,
        approved_destination_hashes=APPROVED_DESTINATIONS,
    )

    with pytest.raises(VoiceSubmissionUncertain):
        provider.place_call(_request())

    assert len(client.calls.created) == 1


def test_signature_verifier_passes_exact_url_and_complete_form_to_sdk() -> None:
    validator = RecordingValidator(True)
    verifier = TwilioSdkSignatureVerifier(validator)
    form = {
        "CallSid": "CA-test-call-001",
        "CallStatus": "completed",
        "SequenceNumber": "3",
    }
    url = "https://api.example.test/v1/twilio/status/opaque-token"

    verifier.verify_or_raise(url=url, form=form, signature="signed-value")

    assert validator.received == (url, form, "signed-value")
    assert verifier.validate(url, form, "signed-value") is True


def test_live_provider_rejects_destination_outside_server_allowlist() -> None:
    client = FakeClient()
    provider = TwilioSdkVoiceProvider(
        client=client,
        secret_resolver=SecretResolver(),
        from_number_secret_ref="secret://dispatch/from",
        public_base_url="https://api.example.test",
        enabled=True,
        approved_destination_hashes=frozenset(
            {hashlib.sha256(b"+15555550999").hexdigest()}
        ),
    )

    with pytest.raises(DispatchConfigurationError) as caught:
        provider.place_call(_request())

    assert caught.value.code == "twilio_destination_not_approved"
    assert client.calls.created == []


def test_invalid_signature_is_a_safe_403_error() -> None:
    verifier = TwilioSdkSignatureVerifier(RecordingValidator(False))
    with pytest.raises(InvalidWebhookSignature) as caught:
        verifier.verify_or_raise(
            url="https://api.example.test/v1/twilio/status/token",
            form={"CallSid": "CA-test-call-001"},
            signature="invalid-secret-looking-value",
        )
    assert caught.value.http_status == 403
    assert "invalid-secret-looking-value" not in repr(caught.value)
    assert "invalid-secret-looking-value" not in str(caught.value)


def test_twiml_is_deterministic_bounded_and_contains_acknowledgement_controls() -> None:
    xml = render_voice_twiml(
        _script(),
        gather_url="https://api.example.test/v1/twilio/gather/token",
        gather_timeout_seconds=10,
    )
    root = fromstring(xml)
    gather = root.find("Gather")

    assert root.tag == "Response"
    assert gather is not None
    assert gather.attrib == {
        "action": "https://api.example.test/v1/twilio/gather/token",
        "method": "POST",
        "numDigits": "1",
        "timeout": "10",
    }
    message = gather.findtext("Say")
    assert message is not None
    assert "Human-confirmed traffic safety incident" in message
    assert "Press 1" in message and "press 2" in message
    assert "record" not in xml.lower()


def test_secret_and_phone_fields_are_redacted_from_representations() -> None:
    contact = ResponseContact(
        contact_id="primary-a",
        tenant_id="00000000-0000-4000-8000-000000000001",
        zone_id="demo-zone-a",
        role=ContactRole.PRIMARY,
        phone_secret_ref="secret://dispatch/sensitive-reference",
        display_name="Sensitive Person Name",
        opted_in_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    request = _request()
    provider = TwilioSdkVoiceProvider(
        client=FakeClient(),
        secret_resolver=SecretResolver(),
        from_number_secret_ref="secret://dispatch/from",
        public_base_url="https://api.example.test",
    )

    assert "sensitive-reference" not in repr(contact)
    assert "Sensitive Person Name" not in repr(contact)
    assert request.destination_secret_ref not in repr(request)
    assert request.callback_token not in repr(request)
    assert "dispatch/from" not in repr(provider)
