"""Tenant-scoped in-memory services for the Phase 2 development slice.

The interfaces are intentionally storage-neutral so PostgreSQL-backed services
can replace these stores without changing route authorization semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Callable
import uuid

from fastapi import HTTPException


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AuditEvent:
    audit_id: str
    tenant_id: str
    principal_id: str
    request_id: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    occurred_at: str


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def record(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        request_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str = "succeeded",
    ) -> AuditEvent:
        event = AuditEvent(
            audit_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            principal_id=principal_id,
            request_id=request_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            occurred_at=_utc_now(),
        )
        with self._lock:
            self._events.append(event)
        return event

    def for_tenant(self, tenant_id: str) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(event for event in self._events if event.tenant_id == tenant_id)


class IdempotencyStore:
    """Bind each mutation key to tenant, operation, and request payload."""

    def __init__(self) -> None:
        self._results: dict[tuple[str, str, str], tuple[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(payload: Any) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def execute(
        self,
        *,
        tenant_id: str,
        operation: str,
        key: str | None,
        payload: Any,
        action: Callable[[], Any],
    ) -> Any:
        if key is None or not 8 <= len(key) <= 200:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "idempotency_key_required",
                    "message": "Idempotency-Key must contain 8 to 200 characters",
                },
            )
        identity = (tenant_id, operation, key)
        digest = self._digest(payload)
        with self._lock:
            existing = self._results.get(identity)
            if existing is not None:
                existing_digest, result = existing
                if existing_digest != digest:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "idempotency_conflict",
                            "message": "Idempotency key was already used for a different request",
                        },
                    )
                return result
            result = action()
            self._results[identity] = (digest, result)
            return result
