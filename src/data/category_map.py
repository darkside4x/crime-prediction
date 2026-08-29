"""Versioned source-category normalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import IngestionError


@dataclass(frozen=True)
class CategoryMap:
    schema_version: str
    canonical_categories: tuple[str, ...]
    aliases: dict[str, str]
    unknown_policy: str

    @classmethod
    def from_file(cls, path: Path) -> "CategoryMap":
        payload = json.loads(path.read_text(encoding="utf-8"))
        canonical = tuple(str(value).strip().lower() for value in payload["canonical_categories"])
        aliases = {
            str(source).strip().lower(): str(target).strip().lower()
            for source, target in payload.get("aliases", {}).items()
        }
        unknown_policy = str(payload.get("unknown_policy", "reject")).lower()
        if unknown_policy not in {"reject", "other"}:
            raise ValueError("unknown_policy must be 'reject' or 'other'")
        invalid_targets = sorted(set(aliases.values()) - set(canonical))
        if invalid_targets:
            raise ValueError(f"Aliases target unknown categories: {invalid_targets}")
        if unknown_policy == "other" and "other" not in canonical:
            raise ValueError("The 'other' policy requires an 'other' canonical category")
        return cls(
            schema_version=str(payload["schema_version"]),
            canonical_categories=canonical,
            aliases=aliases,
            unknown_policy=unknown_policy,
        )

    def normalize(self, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IngestionError("category_invalid", "Event category must be a non-empty string")
        normalized = " ".join(value.strip().lower().split())
        if normalized in self.canonical_categories:
            return normalized
        if normalized in self.aliases:
            return self.aliases[normalized]
        if self.unknown_policy == "other":
            return "other"
        raise IngestionError("category_unmapped", "Event category is not in the configured taxonomy")
