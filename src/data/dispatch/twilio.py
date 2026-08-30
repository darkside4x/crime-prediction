"""Fail-closed Twilio voice adapter, mock provider, and webhook verification."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol
from urllib.parse import quote, urlparse
from xml.etree.ElementTree import Element, SubElement, tostring

from .errors import (
    DispatchConfigurationError,
    DispatchValidationError,
    InvalidWebhookSignature,
    VoiceProviderUnavailable,
    VoiceSubmissionUncertain,
)
from .models import ContactRole, require_identifier, require_utc

_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_SAFE_CASE_REFERENCE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,31}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &'()/-]{0,79}$")


class TwilioMode(StrEnum):
    MOCK = "mock"
    SANDBOX = "sandbox"
    LIVE = "live"


@dataclass(frozen=True, repr=False)
class CallScript:
    case_reference: str
    category: str
    broad_location_label: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not _SAFE_CASE_REFERENCE.fullmatch(self.case_reference):
            raise DispatchValidationError(
                "dispatch_call_script_invalid", "Call case reference is invalid"
            )
        require_identifier(self.category, "category")
        if not _SAFE_LABEL.fullmatch(self.broad_location_label):
            raise DispatchValidationError(
                "dispatch_call_script_invalid", "Call location label is invalid"
            )
        require_utc(self.occurred_at, "occurred_at")

    @property
    def message(self) -> str:
        occurred = require_utc(self.occurred_at, "occurred_at").strftime(
            "%H:%M UTC on %d %B %Y"
        )
        category = self.category.replace("_", " ")
        return (
            "CivicHalo demo alert. "
            f"Human-confirmed {category} incident, case {self.case_reference}, "
            f"near {self.broad_location_label} at {occurred}. "
            "Press 1 to acknowledge, or press 2 to request a human callback."
        )

    def __repr__(self) -> str:
        return f"CallScript(case_reference={self.case_reference!r})"


@dataclass(frozen=True, repr=False)
class OutboundCallRequest:
    request_id: str
    destination_secret_ref: str
    callback_token: str
    attempt_number: int
    target_role: ContactRole
    script: CallScript
    ring_timeout_seconds: int

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        if not self.destination_secret_ref.startswith("secret://"):
            raise DispatchValidationError(
                "dispatch_call_request_invalid", "Destination reference is invalid"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.callback_token):
            raise DispatchValidationError(
                "dispatch_call_request_invalid", "Callback token is invalid"
            )
        if self.attempt_number not in {1, 2, 3}:
            raise DispatchValidationError(
                "dispatch_call_request_invalid", "Attempt number is invalid"
            )
        expected = (
            ContactRole.PRIMARY if self.attempt_number <= 2 else ContactRole.SUPERVISOR
        )
        if self.target_role is not expected:
            raise DispatchValidationError(
                "dispatch_call_request_invalid", "Call target role is invalid"
            )
        if not 5 <= self.ring_timeout_seconds <= 60:
            raise DispatchValidationError(
                "dispatch_call_request_invalid", "Ring timeout is invalid"
            )

    def __repr__(self) -> str:
        return (
            "OutboundCallRequest("
            f"request_id={self.request_id!r}, attempt_number={self.attempt_number!r}, "
            f"target_role={self.target_role.value!r})"
        )


@dataclass(frozen=True, repr=False)
class OutboundCallResult:
    provider_call_reference: str

    def __post_init__(self) -> None:
        if not self.provider_call_reference:
            raise DispatchValidationError(
                "dispatch_provider_response_invalid",
                "Provider call reference is invalid",
            )

    def __repr__(self) -> str:
        return "OutboundCallResult(provider_call_reference=<redacted>)"


class VoiceProvider(Protocol):
    """External-call boundary with explicit submission certainty semantics.

    Implementations may raise an ordinary ``DispatchError`` only when no call
    was submitted. Once submission may have reached the provider, they must
    raise ``VoiceSubmissionUncertain`` so the coordinator never redials.
    """

    def place_call(self, request: OutboundCallRequest) -> OutboundCallResult: ...

    def cancel_call(self, provider_call_reference: str) -> None: ...


class PhoneSecretResolver(Protocol):
    def resolve(self, secret_reference: str) -> str: ...


class MockTwilioVoiceProvider:
    """Idempotent no-network provider used for tests and the default demo mode."""

    def __init__(self, *, failures_before_success: int = 0) -> None:
        self.failures_remaining = max(0, failures_before_success)
        self.place_call_invocations = 0
        self._results: dict[str, OutboundCallResult] = {}
        self._requests: list[OutboundCallRequest] = []
        self._canceled: set[str] = set()
        self._lock = RLock()

    @property
    def requests(self) -> tuple[OutboundCallRequest, ...]:
        with self._lock:
            return tuple(self._requests)

    @property
    def canceled_references(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._canceled)

    def place_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        with self._lock:
            self.place_call_invocations += 1
            existing = self._results.get(request.request_id)
            if existing is not None:
                return existing
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise VoiceProviderUnavailable("voice_provider_mock_failure")
            result = OutboundCallResult(
                provider_call_reference=self.reference_for_request(request.request_id)
            )
            self._results[request.request_id] = result
            self._requests.append(request)
            return result

    @staticmethod
    def reference_for_request(request_id: str) -> str:
        """Return a restart-stable mock ID without persisting a provider SID."""

        require_identifier(request_id, "request_id")
        return f"mock-call-{request_id}"

    def cancel_call(self, provider_call_reference: str) -> None:
        if provider_call_reference:
            with self._lock:
                self._canceled.add(provider_call_reference)


class TwilioSdkVoiceProvider:
    """Live adapter around Twilio's official Python SDK client.

    Composition must deliberately pass ``enabled=True``. Merely installing the
    SDK or supplying credentials cannot enable outbound calls.
    """

    def __init__(
        self,
        *,
        client: Any,
        secret_resolver: PhoneSecretResolver,
        from_number_secret_ref: str,
        public_base_url: str,
        enabled: bool = False,
        approved_destination_hashes: frozenset[str] = frozenset(),
    ) -> None:
        parsed = urlparse(public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise DispatchConfigurationError()
        if not from_number_secret_ref.startswith("secret://"):
            raise DispatchConfigurationError()
        if any(
            re.fullmatch(r"[a-f0-9]{64}", item) is None
            for item in approved_destination_hashes
        ):
            raise DispatchConfigurationError("twilio_destination_allowlist_invalid")
        if enabled and not approved_destination_hashes:
            raise DispatchConfigurationError("twilio_destination_allowlist_empty")
        self._client = client
        self._secret_resolver = secret_resolver
        self._from_number_secret_ref = from_number_secret_ref
        self._public_base_url = public_base_url.rstrip("/")
        self._enabled = enabled
        self._approved_destination_hashes = approved_destination_hashes

    def __repr__(self) -> str:
        return f"TwilioSdkVoiceProvider(enabled={self._enabled!r})"

    def place_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        if not self._enabled:
            raise DispatchConfigurationError("twilio_live_calls_disabled")
        destination = self._resolve_number(request.destination_secret_ref)
        destination_hash = hashlib.sha256(destination.encode("utf-8")).hexdigest()
        if not any(
            hmac.compare_digest(destination_hash, approved)
            for approved in self._approved_destination_hashes
        ):
            raise DispatchConfigurationError("twilio_destination_not_approved")
        origin = self._resolve_number(self._from_number_secret_ref)
        token = quote(request.callback_token, safe="")
        try:
            call = self._client.calls.create(
                to=destination,
                from_=origin,
                url=f"{self._public_base_url}/v1/twilio/voice/{token}",
                method="POST",
                status_callback=f"{self._public_base_url}/v1/twilio/status/{token}",
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                machine_detection="Enable",
                async_amd=True,
                async_amd_status_callback=(
                    f"{self._public_base_url}/v1/twilio/amd/{token}"
                ),
                async_amd_status_callback_method="POST",
                timeout=request.ring_timeout_seconds,
                record=False,
            )
            reference = str(call.sid)
            if not reference:
                raise ValueError("missing call reference")
            return OutboundCallResult(provider_call_reference=reference)
        except Exception:  # noqa: BLE001 - SDK failures are normalized
            # Once ``calls.create`` has started, a timeout or transport error
            # cannot prove that Twilio rejected the request.  Classifying this
            # as an ordinary retryable outage could create a duplicate call.
            raise VoiceSubmissionUncertain() from None

    def cancel_call(self, provider_call_reference: str) -> None:
        if not self._enabled:
            raise DispatchConfigurationError("twilio_live_calls_disabled")
        try:
            self._client.calls(provider_call_reference).update(status="completed")
        except Exception:  # noqa: BLE001 - SDK failures are normalized
            raise VoiceProviderUnavailable() from None

    def _resolve_number(self, reference: str) -> str:
        try:
            value = self._secret_resolver.resolve(reference)
        except Exception:  # noqa: BLE001 - secret-store failures are normalized
            raise DispatchConfigurationError(
                "twilio_phone_secret_unavailable"
            ) from None
        if not _E164.fullmatch(value):
            raise DispatchConfigurationError("twilio_phone_secret_invalid")
        return value


def render_voice_twiml(
    script: CallScript,
    *,
    gather_url: str,
    gather_timeout_seconds: int = 10,
) -> str:
    """Render deterministic, recording-free TwiML for one registered call."""

    parsed = urlparse(gather_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise DispatchValidationError(
            "dispatch_callback_url_invalid", "Gather callback URL must use HTTPS"
        )
    if not 3 <= gather_timeout_seconds <= 30:
        raise DispatchValidationError(
            "dispatch_call_script_invalid", "Gather timeout is invalid"
        )
    response = Element("Response")
    gather = SubElement(
        response,
        "Gather",
        {
            "action": gather_url,
            "method": "POST",
            "numDigits": "1",
            "timeout": str(gather_timeout_seconds),
        },
    )
    SubElement(gather, "Say").text = script.message
    SubElement(response, "Say").text = "No acknowledgement was received. Goodbye."
    return tostring(response, encoding="unicode", short_empty_elements=True)


class SignatureValidator(Protocol):
    def validate(self, url: str, params: Mapping[str, Any], signature: str) -> bool: ...


class WebhookSignatureVerifier(Protocol):
    def validate(self, uri: str, params: Mapping[str, Any], signature: str) -> bool: ...

    def verify(self, *, url: str, form: Mapping[str, Any], signature: str) -> bool: ...

    def verify_or_raise(
        self, *, url: str, form: Mapping[str, Any], signature: str
    ) -> None: ...


class TwilioSdkSignatureVerifier:
    """Adapter preserving the official SDK's exact URL/body semantics."""

    def __init__(self, validator: SignatureValidator) -> None:
        self._validator = validator

    @classmethod
    def from_auth_token(cls, auth_token: str) -> TwilioSdkSignatureVerifier:
        if not auth_token:
            raise DispatchConfigurationError("twilio_auth_token_missing")
        try:
            from twilio.request_validator import RequestValidator
        except ImportError as error:  # pragma: no cover - optional live dependency
            raise DispatchConfigurationError("twilio_sdk_unavailable") from error
        return cls(RequestValidator(auth_token))

    def __repr__(self) -> str:
        return "TwilioSdkSignatureVerifier(validator=<redacted>)"

    def verify(self, *, url: str, form: Mapping[str, Any], signature: str) -> bool:
        if not signature:
            return False
        # Do not normalize, sort, filter, or reconstruct these values. Twilio's
        # validator must receive the externally visible URL and complete form.
        try:
            return bool(self._validator.validate(url, form, signature))
        except Exception:  # noqa: BLE001 - verifier failures always fail closed
            return False

    def validate(self, uri: str, params: Mapping[str, Any], signature: str) -> bool:
        """Match ``RequestValidator.validate`` for direct FastAPI injection."""

        return self.verify(url=uri, form=params, signature=signature)

    def verify_or_raise(
        self, *, url: str, form: Mapping[str, Any], signature: str
    ) -> None:
        if not self.verify(url=url, form=form, signature=signature):
            raise InvalidWebhookSignature()
