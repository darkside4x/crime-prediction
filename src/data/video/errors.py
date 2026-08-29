"""Safe, classified errors for the restricted video worker boundary."""

from __future__ import annotations


class VideoPipelineError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

