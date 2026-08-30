"""Durable, secret-minimizing platform security adapters."""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any


class PostgresRateLimiter:
    """Atomic fixed-window limiter shared by every API replica.

    Only a one-way digest of the middleware's already pseudonymous key is
    stored. The table contains no tenant records and is intentionally accessed
    through ``system_transaction`` rather than an RLS tenant transaction.
    """

    development_only = False

    def __init__(self, database: Any, requests: int, window_seconds: int) -> None:
        if requests < 1 or window_seconds < 1:
            raise ValueError("Rate limit and window must be positive")
        self.database = database
        self.requests = requests
        self.window_seconds = window_seconds

    def allow(self, key: str, *, now: float | None = None) -> tuple[bool, int]:
        current = time.time() if now is None else now
        bucket = math.floor(current / self.window_seconds) * self.window_seconds
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self.database.system_transaction() as cursor:
            if (int(key_hash[:4], 16) + int(current)) % 128 == 0:
                cursor.execute(
                    "DELETE FROM api_rate_limit_buckets "
                    "WHERE expires_at < to_timestamp(%s)",
                    (current,),
                )
            cursor.execute(
                "DELETE FROM api_rate_limit_buckets WHERE key_hash=%s AND window_start < %s",
                (key_hash, bucket),
            )
            cursor.execute(
                """INSERT INTO api_rate_limit_buckets
                   (key_hash,window_start,request_count,expires_at)
                   VALUES (%s,%s,1,to_timestamp(%s))
                   ON CONFLICT (key_hash,window_start) DO UPDATE
                   SET request_count=api_rate_limit_buckets.request_count + 1
                   WHERE api_rate_limit_buckets.request_count < %s
                   RETURNING request_count""",
                (key_hash, bucket, bucket + self.window_seconds * 2, self.requests),
            )
            allowed = cursor.fetchone() is not None
        retry_after = (
            0 if allowed else max(1, math.ceil(bucket + self.window_seconds - current))
        )
        return allowed, retry_after


class PostgresActiveTenantStore:
    """Cross-replica OIDC active-tenant selection without storing subject PII."""

    development_only = False

    def __init__(self, database: Any) -> None:
        self.database = database

    @staticmethod
    def _principal_hash(principal_id: str) -> str:
        if not 1 <= len(principal_id) <= 200:
            raise ValueError("principal_id is outside the supported range")
        return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()

    def get(self, principal_id: str) -> str | None:
        with self.database.system_transaction() as cursor:
            cursor.execute(
                "SELECT tenant_id FROM principal_active_tenants WHERE principal_hash=%s",
                (self._principal_hash(principal_id),),
            )
            row = cursor.fetchone()
        return str(row["tenant_id"]) if row else None

    def set(self, principal_id: str, tenant_id: str) -> None:
        with self.database.system_transaction() as cursor:
            cursor.execute(
                """INSERT INTO principal_active_tenants
                   (principal_hash,tenant_id,updated_at)
                   VALUES (%s,%s,now())
                   ON CONFLICT (principal_hash) DO UPDATE
                   SET tenant_id=excluded.tenant_id,updated_at=excluded.updated_at""",
                (self._principal_hash(principal_id), tenant_id),
            )
