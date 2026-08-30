"""Typed, non-sensitive failures for the dispatch boundary."""

from __future__ import annotations


class DispatchError(Exception):
    """A stable error safe to return across an API boundary.

    Error messages deliberately contain no contact names, telephone numbers,
    provider identifiers, callback tokens, or secret references.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"retryable={self.retryable!r}, http_status={self.http_status!r})"
        )


class DispatchValidationError(DispatchError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, http_status=422)


class DispatchNotAuthorized(DispatchError):
    def __init__(self) -> None:
        super().__init__(
            "dispatch_not_authorized",
            "An explicit call authorization for a confirmed incident is required",
            http_status=409,
        )


class DispatchContactUnavailable(DispatchError):
    def __init__(self, *, ambiguous: bool = False, retryable: bool = False) -> None:
        super().__init__(
            "dispatch_directory_ambiguous"
            if ambiguous
            else "dispatch_contact_unavailable",
            "The response directory cannot resolve the required escalation contacts",
            retryable=retryable,
            http_status=409,
        )


class DispatchResourceNotFound(DispatchError):
    def __init__(self) -> None:
        # The same response is used for absent and cross-tenant resources to avoid
        # turning identifiers into an enumeration oracle.
        super().__init__(
            "dispatch_resource_not_found",
            "The requested dispatch resource is unavailable",
            http_status=404,
        )


class DispatchIdempotencyConflict(DispatchError):
    def __init__(self) -> None:
        super().__init__(
            "dispatch_idempotency_conflict",
            "The idempotency key was already used for a different request",
            http_status=409,
        )


class DispatchStateConflict(DispatchError):
    def __init__(self, code: str = "dispatch_state_conflict") -> None:
        super().__init__(
            code,
            "The dispatch case changed and the operation cannot be applied",
            retryable=code == "dispatch_repository_conflict",
            http_status=409,
        )


class DispatchRetryNotDue(DispatchError):
    def __init__(self) -> None:
        super().__init__(
            "dispatch_retry_not_due",
            "The next escalation attempt is not due yet",
            retryable=True,
            http_status=409,
        )


class VoiceProviderUnavailable(DispatchError):
    def __init__(self, code: str = "voice_provider_unavailable") -> None:
        super().__init__(
            code,
            "The voice provider could not start or update the call",
            retryable=True,
            http_status=503,
        )


class VoiceSubmissionUncertain(DispatchError):
    """The provider may have accepted a call but no durable ID was obtained.

    Retrying this condition could place a second physical call.  The dispatch
    coordinator therefore converts it to a terminal manual-follow-up state.
    """

    def __init__(self) -> None:
        super().__init__(
            "voice_submission_uncertain",
            "The voice call submission outcome requires manual verification",
            http_status=503,
        )


class DispatchConfigurationError(DispatchError):
    def __init__(self, code: str = "dispatch_configuration_invalid") -> None:
        super().__init__(
            code,
            "The voice dispatch integration is not safely configured",
            http_status=503,
        )


class InvalidWebhookSignature(DispatchError):
    def __init__(self) -> None:
        super().__init__(
            "twilio_signature_invalid",
            "The webhook signature is invalid",
            http_status=403,
        )


class WebhookCallMismatch(DispatchError):
    def __init__(self) -> None:
        super().__init__(
            "twilio_call_mismatch",
            "The webhook does not match the registered call",
            http_status=403,
        )
