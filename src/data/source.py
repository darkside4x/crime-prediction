"""Tenant-owned source definitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import validate_contract
from .errors import IngestionError


@dataclass(frozen=True)
class SourceDefinition:
    schema_version: str
    tenant_id: str
    source_id: str
    name: str
    kind: str
    status: str
    config: dict[str, Any]
    secret_ref: str | None
    created_at: str
    updated_at: str | None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceDefinition":
        validate_contract("data-source.schema.json", payload)
        return cls(
            schema_version=payload["schema_version"],
            tenant_id=payload["tenant_id"],
            source_id=payload["source_id"],
            name=payload["name"],
            kind=payload["kind"],
            status=payload["status"],
            config=dict(payload["config"]),
            secret_ref=payload.get("secret_ref"),
            created_at=payload["created_at"],
            updated_at=payload.get("updated_at"),
        )

    @classmethod
    def from_file(cls, path: Path) -> "SourceDefinition":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def authorize(self, authenticated_tenant_id: str) -> None:
        if self.tenant_id != authenticated_tenant_id:
            raise IngestionError(
                "source_tenant_mismatch",
                "The source does not belong to the authenticated tenant",
            )
        if self.status not in {"active", "degraded"}:
            raise IngestionError("source_not_active", "The source is not active")

    def public_record(self) -> dict[str, Any]:
        """Return a persistence-safe representation without resolving credentials."""
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "source_id": self.source_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "config": self.config,
            "secret_ref": self.secret_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
