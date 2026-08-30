"""Secret storage and deterministic opaque callback-token helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import DispatchConfigurationError, DispatchResourceNotFound

_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_SECRET_REF_PREFIX = "secret://aws-secrets-manager/"


class PhoneSecretStore(Protocol):
    def create(self, tenant_id: str, contact_id: str, phone_number: str) -> str: ...
    def update(self, secret_reference: str, phone_number: str) -> None: ...
    def delete(self, secret_reference: str) -> None: ...
    def resolve(self, secret_reference: str) -> str: ...


def mask_phone(phone_number: str) -> str:
    if not _E164.fullmatch(phone_number):
        raise DispatchConfigurationError("dispatch_phone_invalid")
    return f"****{phone_number[-4:]}"


class InMemoryPhoneSecretStore:
    development_only = True

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def create(self, tenant_id: str, contact_id: str, phone_number: str) -> str:
        mask_phone(phone_number)
        reference = f"secret://memory/{tenant_id}/{contact_id}"
        self._values[reference] = phone_number
        return reference

    def update(self, secret_reference: str, phone_number: str) -> None:
        mask_phone(phone_number)
        if secret_reference not in self._values:
            raise DispatchResourceNotFound()
        self._values[secret_reference] = phone_number

    def delete(self, secret_reference: str) -> None:
        self._values.pop(secret_reference, None)

    def resolve(self, secret_reference: str) -> str:
        try:
            return self._values[secret_reference]
        except KeyError:
            raise DispatchConfigurationError(
                "dispatch_phone_secret_unavailable"
            ) from None


class AwsPhoneSecretStore:
    """Store callable destinations under a tenant-prefixed Secrets Manager path."""

    development_only = False

    def __init__(
        self,
        *,
        name_prefix: str,
        region_name: str,
        kms_key_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.name_prefix = name_prefix.strip("/")
        if not self.name_prefix or ".." in self.name_prefix:
            raise DispatchConfigurationError("dispatch_secret_prefix_invalid")
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover
                raise DispatchConfigurationError("aws_sdk_unavailable") from error
            client = boto3.client("secretsmanager", region_name=region_name)
        self.client = client
        self.kms_key_id = kms_key_id

    def _name(self, tenant_id: str, contact_id: str) -> str:
        try:
            uuid.UUID(tenant_id)
            uuid.UUID(contact_id)
        except (TypeError, ValueError) as error:
            raise DispatchConfigurationError(
                "dispatch_secret_reference_invalid"
            ) from error
        return f"{self.name_prefix}/{tenant_id}/dispatch-contacts/{contact_id}"

    def _name_from_reference(self, reference: str) -> str:
        if not reference.startswith(_SECRET_REF_PREFIX):
            raise DispatchConfigurationError("dispatch_secret_reference_invalid")
        name = reference[len(_SECRET_REF_PREFIX) :]
        if not name.startswith(f"{self.name_prefix}/") or ".." in name:
            raise DispatchConfigurationError("dispatch_secret_reference_invalid")
        return name

    def create(self, tenant_id: str, contact_id: str, phone_number: str) -> str:
        mask_phone(phone_number)
        name = self._name(tenant_id, contact_id)
        arguments: dict[str, Any] = {
            "Name": name,
            "Description": "Opted-in CivicHalo dispatch contact destination",
            "SecretString": json.dumps(
                {"phone_number": phone_number}, separators=(",", ":")
            ),
            "Tags": [
                {"Key": "application", "Value": "crime-prediction"},
                {"Key": "tenant_id", "Value": tenant_id},
                {"Key": "data_class", "Value": "dispatch-contact"},
            ],
        }
        if self.kms_key_id:
            arguments["KmsKeyId"] = self.kms_key_id
        try:
            self.client.create_secret(**arguments)
        # SDK clients and test doubles expose different exception hierarchies; never
        # leak provider details through the API boundary.
        except Exception:  # noqa: BLE001
            raise DispatchConfigurationError(
                "dispatch_phone_secret_unavailable"
            ) from None
        return f"{_SECRET_REF_PREFIX}{name}"

    def update(self, secret_reference: str, phone_number: str) -> None:
        mask_phone(phone_number)
        try:
            self.client.put_secret_value(
                SecretId=self._name_from_reference(secret_reference),
                SecretString=json.dumps(
                    {"phone_number": phone_number}, separators=(",", ":")
                ),
            )
        # Convert every provider-specific failure into the stable public error code.
        except Exception:  # noqa: BLE001
            raise DispatchConfigurationError(
                "dispatch_phone_secret_unavailable"
            ) from None

    def delete(self, secret_reference: str) -> None:
        try:
            self.client.delete_secret(
                SecretId=self._name_from_reference(secret_reference),
                RecoveryWindowInDays=7,
            )
        # Convert every provider-specific failure into the stable public error code.
        except Exception:  # noqa: BLE001
            raise DispatchConfigurationError(
                "dispatch_phone_secret_unavailable"
            ) from None

    def resolve(self, secret_reference: str) -> str:
        try:
            response = self.client.get_secret_value(
                SecretId=self._name_from_reference(secret_reference)
            )
            document = json.loads(response["SecretString"])
            phone_number = str(document["phone_number"])
            mask_phone(phone_number)
            return phone_number
        # This block also sanitizes malformed secret payloads and invalid phone data.
        except Exception:  # noqa: BLE001
            raise DispatchConfigurationError(
                "dispatch_phone_secret_unavailable"
            ) from None


@dataclass(frozen=True, repr=False)
class DispatchSecretConfig:
    callback_token_secret: str = field(repr=False)
    configured: bool = False
    account_sid: str | None = field(default=None, repr=False)
    auth_token: str | None = field(default=None, repr=False)
    from_number_secret_ref: str | None = field(default=None, repr=False)

    @classmethod
    def from_json(cls, raw: str) -> DispatchSecretConfig:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise DispatchConfigurationError("twilio_secret_invalid") from None
        secret = str(payload.get("callback_token_secret", ""))
        if len(secret) < 32:
            raise DispatchConfigurationError("dispatch_callback_secret_invalid")
        configured = payload.get("configured") is True
        values = {
            "callback_token_secret": secret,
            "configured": configured,
            "account_sid": payload.get("account_sid"),
            "auth_token": payload.get("auth_token"),
            "from_number_secret_ref": payload.get("from_number_secret_ref"),
        }
        if configured and not all(
            isinstance(values[name], str) and values[name]
            for name in ("account_sid", "auth_token", "from_number_secret_ref")
        ):
            raise DispatchConfigurationError("twilio_secret_invalid")
        return cls(**values)

    def __repr__(self) -> str:
        return f"DispatchSecretConfig(configured={self.configured!r})"


class HmacCallbackTokenCodec:
    """Create restart-stable opaque tokens without persisting their raw value."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise DispatchConfigurationError("dispatch_callback_secret_invalid")
        self._key = secret.encode("utf-8")

    def encode(self, attempt_id: str) -> str:
        try:
            identifier = uuid.UUID(attempt_id).bytes
        except (TypeError, ValueError) as error:
            raise DispatchConfigurationError("dispatch_attempt_invalid") from error
        signature = hmac.new(self._key, identifier, hashlib.sha256).digest()[:16]
        return (
            base64.urlsafe_b64encode(identifier + signature).decode("ascii").rstrip("=")
        )


__all__ = [
    "AwsPhoneSecretStore",
    "DispatchSecretConfig",
    "HmacCallbackTokenCodec",
    "InMemoryPhoneSecretStore",
    "PhoneSecretStore",
    "mask_phone",
]
