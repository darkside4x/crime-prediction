"""Load and validate the repository's JSON Schema contracts."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ContractValidationError


PACKAGE_SCHEMA_DIRECTORY = Path(__file__).resolve().parent / "schemas"
REPOSITORY_SCHEMA_DIRECTORY = Path(__file__).resolve().parents[2] / "contracts" / "schemas"
SCHEMA_DIRECTORY = (
    PACKAGE_SCHEMA_DIRECTORY
    if PACKAGE_SCHEMA_DIRECTORY.is_dir()
    else REPOSITORY_SCHEMA_DIRECTORY
)


@lru_cache(maxsize=None)
def validator_for(schema_name: str) -> Draft202012Validator:
    schema_path = SCHEMA_DIRECTORY / schema_name
    if not schema_path.is_file():
        raise FileNotFoundError(f"Unknown contract: {schema_name}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_contract(schema_name: str, value: Any) -> None:
    errors = sorted(validator_for(schema_name).iter_errors(value), key=lambda error: list(error.path))
    if not errors:
        return
    problems: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        problems.append(f"{location}: {error.message}")
    raise ContractValidationError(schema_name, problems)
