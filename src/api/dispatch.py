"""Role-safe dispatch HTTP boundary and signed Twilio webhook routes.

The module deliberately contains no persistence or provider construction.  A
production composition injects a tenant-scoped, durable service, the existing
idempotency store, and Twilio's ``RequestValidator`` (or a compatible adapter).
Browser routes derive tenant scope from authenticated claims.  Webhook routes
never use browser authentication or callback-supplied tenant identifiers; the
service resolves the stored call mapping from an opaque path token.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import problem
from .tenancy import TenantContext, require_owner, require_reviewer

ContactRole = Literal["primary", "supervisor"]
DispatchState = Literal[
    "queued",
    "dialing",
    "ringing",
    "answered",
    "acknowledged",
    "retry_scheduled",
    "escalated",
    "manual_follow_up",
    "unacknowledged",
    "failed",
    "canceled",
]
AttemptState = Literal[
    "queued",
    "dialing",
    "ringing",
    "answered",
    "acknowledged",
    "retry_scheduled",
    "manual_follow_up",
    "unacknowledged",
    "failed",
    "canceled",
]
TwilioMode = Literal["mock", "sandbox", "live"]

_TIME_PATTERN = r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_E164_PATTERN = r"^\+[1-9][0-9]{7,14}$"
_MASKED_PHONE_PATTERN = r"^[*•]{4} ?[0-9]{4}$"
_OPAQUE_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{20,200}$"
_MAX_WEBHOOK_FIELDS = 64
OpaqueCallToken = Annotated[
    str,
    Path(min_length=20, max_length=200, pattern=_OPAQUE_TOKEN_PATTERN),
]
OwnerContext = Annotated[TenantContext, Depends(require_owner)]
ReviewerContext = Annotated[TenantContext, Depends(require_reviewer)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseContactCreate(_StrictModel):
    zone_id: str = Field(min_length=1, max_length=120)
    broad_location_label: str = Field(min_length=1, max_length=120)
    coverage_h3_cells: list[str] = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)
    phone_number: SecretStr
    role: ContactRole
    enabled: bool = True
    opted_in_for_demo: bool = False
    timezone: str = Field(min_length=1, max_length=80)
    calling_window_start: str = Field(pattern=_TIME_PATTERN)
    calling_window_end: str = Field(pattern=_TIME_PATTERN)
    last_verified_at: datetime

    @field_validator("coverage_h3_cells")
    @classmethod
    def validate_coverage_cells(cls, value: list[str]) -> list[str]:
        import h3

        if len(set(value)) != len(value) or any(
            not isinstance(cell, str) or not h3.is_valid_cell(cell) for cell in value
        ):
            raise ValueError("coverage_h3_cells must contain unique valid H3 cells")
        return value

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: SecretStr) -> SecretStr:
        import re

        if re.fullmatch(_E164_PATTERN, value.get_secret_value()) is None:
            raise ValueError("phone_number must be an E.164 number")
        return value

    @field_validator("last_verified_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_verified_at must include a UTC offset")
        return value


class ResponseContactPatch(_StrictModel):
    broad_location_label: str | None = Field(default=None, min_length=1, max_length=120)
    coverage_h3_cells: list[str] | None = Field(
        default=None, min_length=1, max_length=256
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    phone_number: SecretStr | None = None
    role: ContactRole | None = None
    enabled: bool | None = None
    opted_in_for_demo: bool | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    calling_window_start: str | None = Field(default=None, pattern=_TIME_PATTERN)
    calling_window_end: str | None = Field(default=None, pattern=_TIME_PATTERN)
    last_verified_at: datetime | None = None

    @field_validator("coverage_h3_cells")
    @classmethod
    def validate_coverage_cells(cls, value: list[str] | None) -> list[str] | None:
        import h3

        if value is not None and (
            len(set(value)) != len(value)
            or any(
                not isinstance(cell, str) or not h3.is_valid_cell(cell)
                for cell in value
            )
        ):
            raise ValueError("coverage_h3_cells must contain unique valid H3 cells")
        return value

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: SecretStr | None) -> SecretStr | None:
        import re

        if (
            value is not None
            and re.fullmatch(_E164_PATTERN, value.get_secret_value()) is None
        ):
            raise ValueError("phone_number must be an E.164 number")
        return value

    @field_validator("last_verified_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("last_verified_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def require_change(self) -> ResponseContactPatch:
        if not self.model_fields_set:
            raise ValueError("At least one contact field must be supplied")
        return self


class ResponseContactView(_StrictModel):
    contact_id: str = Field(min_length=1, max_length=120)
    zone_id: str = Field(min_length=1, max_length=120)
    broad_location_label: str = Field(min_length=1, max_length=120)
    coverage_h3_cells: list[str] = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)
    phone_masked: str = Field(
        min_length=3,
        max_length=32,
        json_schema_extra={"pattern": _MASKED_PHONE_PATTERN},
    )
    role: ContactRole
    enabled: bool
    opted_in_for_demo: bool
    timezone: str = Field(min_length=1, max_length=80)
    calling_window_start: str = Field(pattern=_TIME_PATTERN)
    calling_window_end: str = Field(pattern=_TIME_PATTERN)
    last_verified_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("phone_masked")
    @classmethod
    def enforce_masked_phone(cls, value: str) -> str:
        if re.fullmatch(_MASKED_PHONE_PATTERN, value) is None:
            raise ValueError(
                "phone_masked may reveal at most four trailing digits and must use "
                "a supported mask"
            )
        return value


class ResponseContactPage(_StrictModel):
    items: list[ResponseContactView] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=500)


class TestCallRequest(_StrictModel):
    authorize_test_call: Literal[True]


class TestCallView(_StrictModel):
    test_call_id: str = Field(min_length=1, max_length=120)
    contact_id: str = Field(min_length=1, max_length=120)
    contact_name: str = Field(min_length=1, max_length=120)
    phone_masked: str = Field(
        min_length=3,
        max_length=32,
        json_schema_extra={"pattern": _MASKED_PHONE_PATTERN},
    )
    state: Literal["queued", "simulated"]
    created_at: datetime

    @field_validator("phone_masked")
    @classmethod
    def enforce_masked_phone(cls, value: str) -> str:
        return ResponseContactView.enforce_masked_phone(value)


class DispatchAuthorizationRequest(_StrictModel):
    authorize_call: Literal[True]
    message_template_version: str = Field(
        default="dispatch-alert-v1", pattern=_VERSION_PATTERN
    )


class CancelDispatchRequest(_StrictModel):
    cancel_pending_calls: Literal[True] = True
    reason: str = Field(min_length=1, max_length=500)


class DispatchContactSummary(_StrictModel):
    display_name: str = Field(min_length=1, max_length=120)
    phone_masked: str = Field(
        min_length=3,
        max_length=32,
        json_schema_extra={"pattern": _MASKED_PHONE_PATTERN},
    )
    role: ContactRole

    @field_validator("phone_masked")
    @classmethod
    def enforce_masked_phone(cls, value: str) -> str:
        return ResponseContactView.enforce_masked_phone(value)


class DispatchAttemptView(_StrictModel):
    attempt_id: str = Field(min_length=1, max_length=120)
    attempt_number: int = Field(ge=1, le=3)
    target_role: ContactRole
    contact_name: str = Field(min_length=1, max_length=120)
    phone_masked: str = Field(
        min_length=3,
        max_length=32,
        json_schema_extra={"pattern": _MASKED_PHONE_PATTERN},
    )
    state: AttemptState
    safe_error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,80}$")
    created_at: datetime
    updated_at: datetime

    @field_validator("phone_masked")
    @classmethod
    def enforce_masked_phone(cls, value: str) -> str:
        return ResponseContactView.enforce_masked_phone(value)

    @model_validator(mode="after")
    def enforce_escalation_role(self) -> DispatchAttemptView:
        expected_role: ContactRole = (
            "supervisor" if self.attempt_number == 3 else "primary"
        )
        if self.target_role != expected_role:
            raise ValueError(
                f"attempt {self.attempt_number} must target the {expected_role} contact"
            )
        return self


class DispatchCaseView(_StrictModel):
    dispatch_case_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    case_reference: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=80)
    zone_label: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    state: DispatchState
    message_template_version: str = Field(pattern=_VERSION_PATTERN)
    authorized_by_principal_id: str = Field(min_length=1, max_length=200)
    authorized_at: datetime
    primary_contact: DispatchContactSummary
    supervisor_contact: DispatchContactSummary
    attempts: list[DispatchAttemptView] = Field(default_factory=list, max_length=3)
    next_attempt_at: datetime | None = None
    canceled_at: datetime | None = None


class DispatchPreviewView(_StrictModel):
    incident_id: str = Field(min_length=1, max_length=120)
    case_reference: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=80)
    zone_label: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    primary_contact: DispatchContactSummary
    supervisor_contact: DispatchContactSummary
    maximum_attempts: Literal[3] = 3
    retry_delay_seconds: int = Field(ge=5, le=3600)


class VoicePrompt(_StrictModel):
    """Safe, deterministic text selected from persisted confirmed facts."""

    message: str = Field(min_length=1, max_length=500)
    language: Literal["en-US", "en-IN"] = "en-US"
    acknowledgement_timeout_seconds: int = Field(default=10, ge=5, le=30)


class DispatchApiError(HTTPException):
    """Typed error that a storage/provider adapter may raise at this boundary."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail={
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": [],
            },
        )


class IdempotencyExecutor(Protocol):
    def execute(
        self,
        *,
        tenant_id: str,
        operation: str,
        key: str | None,
        payload: Any,
        action: Any,
    ) -> Any: ...


class TwilioSignatureVerifier(Protocol):
    """Matches ``twilio.request_validator.RequestValidator.validate``."""

    def validate(self, uri: str, params: Mapping[str, str], signature: str) -> bool: ...


class DispatchApiService(Protocol):
    """Storage/provider-neutral operations required by the HTTP boundary."""

    def list_response_contacts(
        self,
        *,
        tenant_id: str,
        zone_id: str | None,
        enabled: bool | None,
        limit: int,
        cursor: str | None,
    ) -> ResponseContactPage: ...

    def get_response_contact(
        self, *, tenant_id: str, contact_id: str
    ) -> ResponseContactView: ...

    def create_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact: ResponseContactCreate,
    ) -> ResponseContactView: ...

    def update_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
        changes: ResponseContactPatch,
    ) -> ResponseContactView: ...

    def delete_response_contact(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
    ) -> None: ...

    def create_test_call(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        contact_id: str,
    ) -> TestCallView: ...

    def authorize_dispatch(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        principal_role: str,
        request_id: str,
        incident_id: str,
        idempotency_key: str,
        message_template_version: str,
    ) -> DispatchCaseView: ...

    def preview_dispatch(
        self, *, tenant_id: str, incident_id: str
    ) -> DispatchPreviewView: ...

    def get_dispatch_case(
        self, *, tenant_id: str, dispatch_case_id: str
    ) -> DispatchCaseView: ...

    def cancel_dispatch(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        dispatch_case_id: str,
        reason: str,
    ) -> DispatchCaseView: ...

    def twilio_voice(
        self, *, opaque_call_token: str, form: Mapping[str, str]
    ) -> VoicePrompt: ...

    def twilio_gather(
        self, *, opaque_call_token: str, form: Mapping[str, str]
    ) -> None: ...

    def twilio_amd(
        self, *, opaque_call_token: str, form: Mapping[str, str]
    ) -> None: ...

    def twilio_status(
        self, *, opaque_call_token: str, form: Mapping[str, str]
    ) -> None: ...


@dataclass(frozen=True)
class DispatchApiDependencies:
    service: DispatchApiService
    idempotency: IdempotencyExecutor
    signature_verifier: TwilioSignatureVerifier
    public_base_url: str
    twilio_mode: TwilioMode = "mock"
    test_calls_enabled: bool = False
    external_calls_enabled: bool = False
    webhook_body_limit_bytes: int = 65_536

    def __post_init__(self) -> None:
        parsed = urlsplit(self.public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("public_base_url must be an HTTPS origin or base path")
        if self.webhook_body_limit_bytes < 1:
            raise ValueError("webhook_body_limit_bytes must be positive")
        object.__setattr__(self, "public_base_url", self.public_base_url.rstrip("/"))


def _public_model[ModelT: BaseModel](model: type[ModelT], value: Any) -> ModelT:
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except ValidationError as error:
        raise problem(
            500,
            "dispatch_response_invalid",
            "Dispatch data could not be safely serialized",
        ) from error


def _public_payload[ModelT: BaseModel](
    model: type[ModelT], value: Any
) -> dict[str, Any]:
    """Return a JSON value safe for durable idempotency-result storage."""

    return _public_model(model, value).model_dump(mode="json")


def _mutation_payload(body: BaseModel, **identifiers: str) -> dict[str, Any]:
    return {**identifiers, "body": body.model_dump(mode="json")}


def _canonical_webhook_url(
    dependencies: DispatchApiDependencies, request: Request
) -> str:
    url = f"{dependencies.public_base_url}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


async def _verified_twilio_form(
    dependencies: DispatchApiDependencies, request: Request
) -> dict[str, str]:
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/x-www-form-urlencoded":
        raise problem(
            415, "webhook_content_type_invalid", "Twilio webhooks must be form encoded"
        )
    raw_body = await request.body()
    if len(raw_body) > dependencies.webhook_body_limit_bytes:
        raise problem(
            413, "webhook_too_large", "Webhook body exceeds the configured limit"
        )
    try:
        pairs: Sequence[tuple[str, str]] = parse_qsl(
            raw_body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=_MAX_WEBHOOK_FIELDS,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise problem(400, "webhook_form_invalid", "Webhook form is invalid") from error
    form: dict[str, str] = {}
    for key, value in pairs:
        if key in form:
            raise problem(
                400, "webhook_form_invalid", "Webhook form contains duplicate fields"
            )
        form[key] = value
    signature = request.headers.get("x-twilio-signature", "")
    canonical_url = _canonical_webhook_url(dependencies, request)
    try:
        valid = bool(signature) and dependencies.signature_verifier.validate(
            canonical_url, form, signature
        )
    except Exception:  # noqa: BLE001 - a verifier failure must always fail closed
        valid = False
    if not valid:
        raise problem(403, "invalid_webhook_signature", "Webhook signature is invalid")
    return form


def _twiml_response() -> Response:
    return Response(content="<Response />", media_type="application/xml")


def _voice_twiml(
    dependencies: DispatchApiDependencies,
    opaque_call_token: str,
    prompt: VoicePrompt,
) -> Response:
    root = ElementTree.Element("Response")
    gather_url = f"{dependencies.public_base_url}/v1/twilio/gather/{opaque_call_token}"
    gather = ElementTree.SubElement(
        root,
        "Gather",
        {
            "action": gather_url,
            "method": "POST",
            "input": "dtmf",
            "numDigits": "1",
            "timeout": str(prompt.acknowledgement_timeout_seconds),
        },
    )
    message = ElementTree.SubElement(
        gather, "Say", {"language": prompt.language, "voice": "alice"}
    )
    message.text = prompt.message
    no_input = ElementTree.SubElement(
        root, "Say", {"language": prompt.language, "voice": "alice"}
    )
    no_input.text = "No acknowledgement was received. Goodbye."
    body = ElementTree.tostring(root, encoding="unicode", short_empty_elements=True)
    return Response(content=body, media_type="application/xml")


def create_dispatch_router(dependencies: DispatchApiDependencies) -> APIRouter:
    """Create dispatch routes without mutating or importing the main application."""

    router = APIRouter()

    @router.get("/v1/response-contacts", response_model=ResponseContactPage)
    def list_response_contacts(
        ctx: OwnerContext,
        zone_id: str | None = Query(default=None, min_length=1, max_length=120),
        enabled: bool | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=500),
    ) -> ResponseContactPage:
        result = dependencies.service.list_response_contacts(
            tenant_id=ctx.tenant_id,
            zone_id=zone_id,
            enabled=enabled,
            limit=limit,
            cursor=cursor,
        )
        return _public_model(ResponseContactPage, result)

    @router.post(
        "/v1/response-contacts", status_code=201, response_model=ResponseContactView
    )
    def create_response_contact(
        body: ResponseContactCreate,
        ctx: OwnerContext,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ResponseContactView:
        result = dependencies.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation="response_contact_create",
            key=idempotency_key,
            payload=_mutation_payload(body),
            action=lambda: _public_payload(
                ResponseContactView,
                dependencies.service.create_response_contact(
                    tenant_id=ctx.tenant_id,
                    principal_id=ctx.principal_id,
                    request_id=ctx.request_id,
                    contact=body,
                ),
            ),
        )
        return _public_model(ResponseContactView, result)

    @router.get(
        "/v1/response-contacts/{contact_id}", response_model=ResponseContactView
    )
    def get_response_contact(
        ctx: OwnerContext,
        contact_id: str = Path(min_length=1, max_length=120),
    ) -> ResponseContactView:
        result = dependencies.service.get_response_contact(
            tenant_id=ctx.tenant_id, contact_id=contact_id
        )
        return _public_model(ResponseContactView, result)

    @router.patch(
        "/v1/response-contacts/{contact_id}", response_model=ResponseContactView
    )
    def update_response_contact(
        body: ResponseContactPatch,
        ctx: OwnerContext,
        contact_id: str = Path(min_length=1, max_length=120),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ResponseContactView:
        result = dependencies.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"response_contact_update:{contact_id}",
            key=idempotency_key,
            payload=_mutation_payload(body, contact_id=contact_id),
            action=lambda: _public_payload(
                ResponseContactView,
                dependencies.service.update_response_contact(
                    tenant_id=ctx.tenant_id,
                    principal_id=ctx.principal_id,
                    request_id=ctx.request_id,
                    contact_id=contact_id,
                    changes=body,
                ),
            ),
        )
        return _public_model(ResponseContactView, result)

    @router.delete("/v1/response-contacts/{contact_id}", status_code=204)
    def delete_response_contact(
        ctx: OwnerContext,
        contact_id: str = Path(min_length=1, max_length=120),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        dependencies.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"response_contact_delete:{contact_id}",
            key=idempotency_key,
            payload={"contact_id": contact_id},
            action=lambda: dependencies.service.delete_response_contact(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                contact_id=contact_id,
            ),
        )
        return Response(status_code=204)

    @router.post(
        "/v1/response-contacts/{contact_id}/test-calls",
        status_code=202,
        response_model=TestCallView,
    )
    def create_test_call(
        body: TestCallRequest,
        ctx: OwnerContext,
        contact_id: str = Path(min_length=1, max_length=120),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TestCallView:
        if not dependencies.test_calls_enabled or dependencies.twilio_mode != "mock":
            raise problem(
                403,
                "test_call_disabled",
                "Test calls are disabled by the deployment safety gate",
            )
        result = dependencies.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"response_contact_test_call:{contact_id}",
            key=idempotency_key,
            payload=_mutation_payload(body, contact_id=contact_id),
            action=lambda: _public_payload(
                TestCallView,
                dependencies.service.create_test_call(
                    tenant_id=ctx.tenant_id,
                    principal_id=ctx.principal_id,
                    request_id=ctx.request_id,
                    contact_id=contact_id,
                ),
            ),
        )
        return _public_model(TestCallView, result)

    @router.post(
        "/v1/incidents/{incident_id}/dispatch-authorizations",
        status_code=201,
        response_model=DispatchCaseView,
    )
    def authorize_dispatch(
        body: DispatchAuthorizationRequest,
        ctx: ReviewerContext,
        incident_id: str = Path(min_length=1, max_length=120),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> DispatchCaseView:
        if (
            dependencies.twilio_mode in {"sandbox", "live"}
            and not dependencies.external_calls_enabled
        ):
            raise problem(
                503,
                "external_calls_disabled",
                "External calls are disabled by the deployment safety gate",
            )
        result = dependencies.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"dispatch_authorize:{incident_id}",
            key=idempotency_key,
            payload=_mutation_payload(body, incident_id=incident_id),
            action=lambda: _public_payload(
                DispatchCaseView,
                dependencies.service.authorize_dispatch(
                    tenant_id=ctx.tenant_id,
                    principal_id=ctx.principal_id,
                    principal_role=ctx.role,
                    request_id=ctx.request_id,
                    incident_id=incident_id,
                    idempotency_key=str(idempotency_key),
                    message_template_version=body.message_template_version,
                ),
            ),
        )
        return _public_model(DispatchCaseView, result)

    @router.get(
        "/v1/incidents/{incident_id}/dispatch-preview",
        response_model=DispatchPreviewView,
    )
    def preview_dispatch(
        ctx: ReviewerContext,
        incident_id: str = Path(min_length=1, max_length=120),
    ) -> DispatchPreviewView:
        result = dependencies.service.preview_dispatch(
            tenant_id=ctx.tenant_id, incident_id=incident_id
        )
        return _public_model(DispatchPreviewView, result)

    @router.get(
        "/v1/dispatch-cases/{dispatch_case_id}", response_model=DispatchCaseView
    )
    def get_dispatch_case(
        ctx: ReviewerContext,
        dispatch_case_id: str = Path(min_length=1, max_length=120),
    ) -> DispatchCaseView:
        result = dependencies.service.get_dispatch_case(
            tenant_id=ctx.tenant_id, dispatch_case_id=dispatch_case_id
        )
        return _public_model(DispatchCaseView, result)

    @router.post(
        "/v1/dispatch-cases/{dispatch_case_id}/cancel",
        response_model=DispatchCaseView,
    )
    def cancel_dispatch(
        body: CancelDispatchRequest,
        ctx: ReviewerContext,
        dispatch_case_id: str = Path(min_length=1, max_length=120),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> DispatchCaseView:
        result = dependencies.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"dispatch_cancel:{dispatch_case_id}",
            key=idempotency_key,
            payload=_mutation_payload(body, dispatch_case_id=dispatch_case_id),
            action=lambda: _public_payload(
                DispatchCaseView,
                dependencies.service.cancel_dispatch(
                    tenant_id=ctx.tenant_id,
                    principal_id=ctx.principal_id,
                    request_id=ctx.request_id,
                    dispatch_case_id=dispatch_case_id,
                    reason=body.reason,
                ),
            ),
        )
        return _public_model(DispatchCaseView, result)

    @router.post("/v1/twilio/voice/{opaque_call_token}", include_in_schema=False)
    async def twilio_voice(
        request: Request, opaque_call_token: OpaqueCallToken
    ) -> Response:
        form = await _verified_twilio_form(dependencies, request)
        prompt = dependencies.service.twilio_voice(
            opaque_call_token=opaque_call_token, form=form
        )
        return _voice_twiml(
            dependencies,
            opaque_call_token,
            _public_model(VoicePrompt, prompt),
        )

    @router.post("/v1/twilio/gather/{opaque_call_token}", include_in_schema=False)
    async def twilio_gather(
        request: Request, opaque_call_token: OpaqueCallToken
    ) -> Response:
        form = await _verified_twilio_form(dependencies, request)
        dependencies.service.twilio_gather(
            opaque_call_token=opaque_call_token, form=form
        )
        return _twiml_response()

    @router.post("/v1/twilio/amd/{opaque_call_token}", include_in_schema=False)
    async def twilio_amd(
        request: Request, opaque_call_token: OpaqueCallToken
    ) -> Response:
        form = await _verified_twilio_form(dependencies, request)
        dependencies.service.twilio_amd(opaque_call_token=opaque_call_token, form=form)
        return _twiml_response()

    @router.post("/v1/twilio/status/{opaque_call_token}", include_in_schema=False)
    async def twilio_status(
        request: Request, opaque_call_token: OpaqueCallToken
    ) -> Response:
        form = await _verified_twilio_form(dependencies, request)
        dependencies.service.twilio_status(
            opaque_call_token=opaque_call_token, form=form
        )
        return _twiml_response()

    return router


__all__ = [
    "CancelDispatchRequest",
    "DispatchApiDependencies",
    "DispatchApiError",
    "DispatchApiService",
    "DispatchAttemptView",
    "DispatchAuthorizationRequest",
    "DispatchCaseView",
    "DispatchContactSummary",
    "DispatchPreviewView",
    "ResponseContactCreate",
    "ResponseContactPage",
    "ResponseContactPatch",
    "ResponseContactView",
    "TestCallView",
    "TwilioSignatureVerifier",
    "VoicePrompt",
    "create_dispatch_router",
]
