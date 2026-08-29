"""JSON Schema validation for model inputs and outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import DataContractError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPOSITORY_ROOT / "contracts" / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_ROOT / f"{name}.schema.json").read_text(encoding="utf-8"))


def validate_contract(name: str, payload: Any) -> None:
    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors[:5]
        )
        raise DataContractError(f"{name} contract validation failed: {details}")
