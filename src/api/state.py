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
    development_only = True

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

    development_only = True

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


class PostgresAuditLog:
    development_only = False

    def __init__(self, database: Any) -> None:
        self.database = database

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
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO api_audit_events
                   (tenant_id,audit_id,principal_id,request_id,action,resource_type,
                    resource_id,outcome,occurred_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    event.tenant_id,
                    event.audit_id,
                    event.principal_id,
                    event.request_id,
                    event.action,
                    event.resource_type,
                    event.resource_id,
                    event.outcome,
                    event.occurred_at,
                ),
            )
        return event

    def for_tenant(self, tenant_id: str) -> tuple[AuditEvent, ...]:
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT * FROM api_audit_events WHERE tenant_id=%s
                   ORDER BY occurred_at, audit_id""",
                (tenant_id,),
            )
            return tuple(
                AuditEvent(
                    audit_id=str(row["audit_id"]),
                    tenant_id=str(row["tenant_id"]),
                    principal_id=row["principal_id"],
                    request_id=str(row["request_id"]),
                    action=row["action"],
                    resource_type=row["resource_type"],
                    resource_id=row["resource_id"],
                    outcome=row["outcome"],
                    occurred_at=row["occurred_at"].isoformat().replace("+00:00", "Z"),
                )
                for row in cursor.fetchall()
            )


class PostgresIdempotencyStore:
    """Durable idempotency reservations with payload binding and retry recovery."""

    development_only = False

    def __init__(self, database: Any) -> None:
        self.database = database

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
        digest = IdempotencyStore._digest(payload)
        owner_token = str(uuid.uuid4())
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO api_idempotency_records
                   (tenant_id,operation,idempotency_key,payload_digest,state,owner_token,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,'running',%s,%s,%s)
                   ON CONFLICT (tenant_id,operation,idempotency_key) DO NOTHING""",
                (tenant_id, operation, key, digest, owner_token, _utc_now(), _utc_now()),
            )
            inserted = cursor.rowcount == 1
            cursor.execute(
                """SELECT payload_digest,state,result,owner_token FROM api_idempotency_records
                   WHERE tenant_id=%s AND operation=%s AND idempotency_key=%s""",
                (tenant_id, operation, key),
            )
            record = cursor.fetchone()
        if record["payload_digest"] != digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_conflict",
                    "message": "Idempotency key was already used for a different request",
                },
            )
        if not inserted:
            if record["state"] == "completed":
                return record["result"]
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_in_progress",
                    "message": "An identical mutation is already in progress",
                    "retryable": True,
                },
            )
        try:
            result = action()
            encoded = json.loads(json.dumps(result, default=str))
            with self.database.transaction(tenant_id) as cursor:
                cursor.execute(
                    """UPDATE api_idempotency_records SET state='completed',result=%s::jsonb,
                       updated_at=%s WHERE tenant_id=%s AND operation=%s
                       AND idempotency_key=%s AND owner_token=%s""",
                    (json.dumps(encoded), _utc_now(), tenant_id, operation, key, owner_token),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Idempotency reservation ownership was lost")
            return result
        except Exception:
            with self.database.transaction(tenant_id) as cursor:
                cursor.execute(
                    """DELETE FROM api_idempotency_records WHERE tenant_id=%s AND operation=%s
                       AND idempotency_key=%s AND owner_token=%s AND state='running'""",
                    (tenant_id, operation, key, owner_token),
                )
            raise
