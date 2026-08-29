"""Typed ingestion failures safe to expose in run summaries."""

from __future__ import annotations


class IngestionError(Exception):
    """A classified ingestion error.

    The message must describe the problem without copying a source payload.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ContractValidationError(IngestionError):
    """Raised when a versioned JSON contract is not satisfied."""

    def __init__(self, schema_name: str, problems: list[str]) -> None:
        summary = "; ".join(problems[:5])
        super().__init__(
            "contract_validation_failed",
            f"{schema_name} failed validation: {summary}",
        )
        self.schema_name = schema_name
        self.problems = problems
