"""Transport-neutral adapter protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


@dataclass(frozen=True)
class SourceHealth:
    healthy: bool
    detail: str


@dataclass(frozen=True)
class AdapterItem:
    checkpoint: str | int
    payload: dict[str, Any] | None
    raw_value: str | None = None
    error_code: str | None = None
    safe_detail: str | None = None


class EventSourceAdapter(Protocol):
    async def validate_connection(self) -> SourceHealth: ...

    async def read(self, checkpoint: str | int | None) -> AsyncIterator[AdapterItem]: ...

    async def commit(self, checkpoint: str | int) -> None: ...
