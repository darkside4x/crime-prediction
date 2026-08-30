"""Fail-closed production composition for voice dispatch."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from src.api.dispatch import DispatchApiDependencies
from src.api.dispatch_application import PostgresDispatchApplicationService
from src.data.postgres import TenantPostgres
from src.data.video.runtime import _secret_value

from .broker import SqsDispatchBroker
from .postgres import PostgresContactDirectory, PostgresDispatchRepository
from .secrets import (
    AwsPhoneSecretStore,
    DispatchSecretConfig,
    HmacCallbackTokenCodec,
)
from .service import DispatchCoordinator, EscalationPolicy
from .twilio import (
    MockTwilioVoiceProvider,
    TwilioMode,
    TwilioSdkSignatureVerifier,
    TwilioSdkVoiceProvider,
)


class RejectAllWebhookSignatures:
    """Mock-mode webhook gate: simulations happen inside the worker, not HTTP."""

    def validate(self, uri: str, params: Mapping[str, str], signature: str) -> bool:
        del uri, params, signature
        return False


@dataclass(frozen=True, repr=False)
class DispatchSettings:
    mode: TwilioMode
    queue_url: str
    queue_dlq_url: str
    public_base_url: str
    aws_region: str
    contact_secret_prefix: str
    contact_kms_key_id: str | None
    secret_config: DispatchSecretConfig = field(repr=False)
    retry_seconds: int = 30
    max_cases_per_tenant_day: int = 20
    worker_lease_seconds: int = 90
    test_calls_enabled: bool = False
    external_calls_enabled: bool = False
    approved_destination_hashes: frozenset[str] = frozenset()

    @classmethod
    def from_environment(cls) -> DispatchSettings:
        def boolean(name: str, default: bool = False) -> bool:
            raw = os.getenv(name, str(default).lower()).strip().lower()
            if raw not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false")
            return raw == "true"

        try:
            mode = TwilioMode(os.getenv("DISPATCH_MODE", "mock").strip().lower())
        except ValueError as error:
            raise ValueError("DISPATCH_MODE must be mock, sandbox, or live") from error
        queue_url = os.getenv("DISPATCH_QUEUE_URL", "").strip()
        queue_dlq_url = os.getenv("DISPATCH_QUEUE_DLQ_URL", "").strip()
        public_base_url = os.getenv("DISPATCH_PUBLIC_BASE_URL", "").strip().rstrip("/")
        aws_region = os.getenv("AWS_REGION", "").strip()
        contact_secret_prefix = (
            os.getenv("DISPATCH_CONTACT_SECRET_PREFIX", "").strip().strip("/")
        )
        raw_secret = _secret_value("DISPATCH_TWILIO_SECRET")
        missing = [
            name
            for name, value in (
                ("DISPATCH_QUEUE_URL", queue_url),
                ("DISPATCH_QUEUE_DLQ_URL", queue_dlq_url),
                ("DISPATCH_PUBLIC_BASE_URL", public_base_url),
                ("AWS_REGION", aws_region),
                ("DISPATCH_CONTACT_SECRET_PREFIX", contact_secret_prefix),
                ("DISPATCH_TWILIO_SECRET", raw_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing dispatch settings: {', '.join(missing)}")
        parsed = urlsplit(public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DISPATCH_PUBLIC_BASE_URL must be an HTTPS base URL")
        retry_seconds = int(os.getenv("DISPATCH_RETRY_SECONDS", "30"))
        max_cases = int(os.getenv("DISPATCH_MAX_CASES_PER_TENANT_DAY", "20"))
        lease_seconds = int(os.getenv("DISPATCH_WORKER_LEASE_SECONDS", "90"))
        if not 5 <= retry_seconds <= 3600:
            raise ValueError("DISPATCH_RETRY_SECONDS must be between 5 and 3600")
        if not 1 <= max_cases <= 1000:
            raise ValueError(
                "DISPATCH_MAX_CASES_PER_TENANT_DAY must be between 1 and 1000"
            )
        if not 30 <= lease_seconds <= 43200:
            raise ValueError(
                "DISPATCH_WORKER_LEASE_SECONDS must be between 30 and 43200"
            )
        test_calls = boolean("DISPATCH_TEST_CALLS_ENABLED")
        external_calls = boolean("DISPATCH_EXTERNAL_CALLS_ENABLED")
        approved_hashes = frozenset(
            value.strip().lower()
            for value in os.getenv("DISPATCH_APPROVED_DESTINATION_SHA256", "").split(
                ","
            )
            if value.strip()
        )
        if any(
            re.fullmatch(r"[a-f0-9]{64}", value) is None for value in approved_hashes
        ):
            raise ValueError(
                "DISPATCH_APPROVED_DESTINATION_SHA256 must contain comma-separated SHA-256 values"
            )
        secret_config = DispatchSecretConfig.from_json(raw_secret)
        if (
            mode in {TwilioMode.SANDBOX, TwilioMode.LIVE}
            and not secret_config.configured
        ):
            raise ValueError(
                "Twilio credentials must be configured for sandbox/live mode"
            )
        if external_calls and mode is TwilioMode.MOCK:
            raise ValueError("External calls cannot be enabled in mock mode")
        if mode in {TwilioMode.SANDBOX, TwilioMode.LIVE} and not approved_hashes:
            raise ValueError(
                "Sandbox/live mode requires at least one approved destination hash"
            )
        return cls(
            mode=mode,
            queue_url=queue_url,
            queue_dlq_url=queue_dlq_url,
            public_base_url=public_base_url,
            aws_region=aws_region,
            contact_secret_prefix=contact_secret_prefix,
            contact_kms_key_id=os.getenv("DISPATCH_CONTACT_KMS_KEY_ID", "").strip()
            or None,
            secret_config=secret_config,
            retry_seconds=retry_seconds,
            max_cases_per_tenant_day=max_cases,
            worker_lease_seconds=lease_seconds,
            test_calls_enabled=test_calls,
            external_calls_enabled=external_calls,
            approved_destination_hashes=approved_hashes,
        )

    def __repr__(self) -> str:
        return (
            "DispatchSettings("
            f"mode={self.mode.value!r}, aws_region={self.aws_region!r}, "
            f"retry_seconds={self.retry_seconds!r})"
        )


@dataclass
class DispatchRuntime:
    settings: DispatchSettings
    directory: PostgresContactDirectory
    repository: PostgresDispatchRepository
    broker: SqsDispatchBroker
    phone_secrets: AwsPhoneSecretStore
    coordinator: DispatchCoordinator
    application: PostgresDispatchApplicationService
    api_dependencies: DispatchApiDependencies


def create_dispatch_runtime(
    settings: DispatchSettings,
    *,
    database: TenantPostgres,
    audit_log: Any,
    idempotency_store: Any,
) -> DispatchRuntime:
    callback_codec = HmacCallbackTokenCodec(
        settings.secret_config.callback_token_secret
    )
    directory = PostgresContactDirectory(database)
    repository = PostgresDispatchRepository(database, callback_tokens=callback_codec)
    broker = SqsDispatchBroker(settings.queue_url, region_name=settings.aws_region)
    phone_secrets = AwsPhoneSecretStore(
        name_prefix=settings.contact_secret_prefix,
        region_name=settings.aws_region,
        kms_key_id=settings.contact_kms_key_id,
    )
    if settings.mode is TwilioMode.MOCK:
        voice_provider = MockTwilioVoiceProvider()
        signature_verifier: Any = RejectAllWebhookSignatures()
    else:
        try:
            from twilio.rest import Client
        except ImportError as error:  # pragma: no cover
            raise RuntimeError(
                "Install the platform extra for Twilio support"
            ) from error
        config = settings.secret_config
        client = Client(config.account_sid, config.auth_token)
        voice_provider = TwilioSdkVoiceProvider(
            client=client,
            secret_resolver=phone_secrets,
            from_number_secret_ref=str(config.from_number_secret_ref),
            public_base_url=settings.public_base_url,
            enabled=settings.external_calls_enabled,
            approved_destination_hashes=settings.approved_destination_hashes,
        )
        signature_verifier = TwilioSdkSignatureVerifier.from_auth_token(
            str(config.auth_token)
        )
    policy = EscalationPolicy(
        retry_delay=timedelta(seconds=settings.retry_seconds),
        ring_timeout_seconds=20,
        gather_timeout_seconds=10,
        provider_submission_timeout=timedelta(seconds=settings.worker_lease_seconds),
        policy_version="voice-escalation-v1",
        message_template_version="dispatch-alert-v1",
    )
    coordinator = DispatchCoordinator(
        repository,
        directory,
        voice_provider,
        policy=policy,
        token_factory=callback_codec.encode,
    )
    application = PostgresDispatchApplicationService(
        database=database,
        directory=directory,
        repository=repository,
        coordinator=coordinator,
        broker=broker,
        phone_secrets=phone_secrets,
        audit_log=audit_log,
        max_cases_per_tenant_day=settings.max_cases_per_tenant_day,
        approved_destination_hashes=settings.approved_destination_hashes,
        enforce_destination_allowlist=(
            settings.mode in {TwilioMode.SANDBOX, TwilioMode.LIVE}
        ),
    )
    api_dependencies = DispatchApiDependencies(
        service=application,
        idempotency=idempotency_store,
        signature_verifier=signature_verifier,
        public_base_url=settings.public_base_url,
        twilio_mode=settings.mode.value,
        test_calls_enabled=settings.test_calls_enabled,
        external_calls_enabled=settings.external_calls_enabled,
    )
    return DispatchRuntime(
        settings,
        directory,
        repository,
        broker,
        phone_secrets,
        coordinator,
        application,
        api_dependencies,
    )


__all__ = [
    "DispatchRuntime",
    "DispatchSettings",
    "RejectAllWebhookSignatures",
    "create_dispatch_runtime",
]
