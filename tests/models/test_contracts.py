from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models.contracts import REPOSITORY_ROOT, validate_contract
from src.models.errors import DataContractError


def test_every_contract_fixture_validates() -> None:
    schema_root = REPOSITORY_ROOT / "contracts" / "schemas"
    fixture_root = REPOSITORY_ROOT / "contracts" / "fixtures"
    for schema_path in schema_root.glob("*.schema.json"):
        name = schema_path.name.removesuffix(".schema.json")
        fixture = json.loads((fixture_root / f"{name}.json").read_text(encoding="utf-8"))
        validate_contract(name, fixture)


def test_reka_fact_contract_rejects_sensitive_or_unsuppressed_values() -> None:
    fixture_path = REPOSITORY_ROOT / "contracts" / "fixtures" / "reka-fact-bundle.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["facts"][0]["latitude"] = 12.9
    with pytest.raises(DataContractError):
        validate_contract("reka-fact-bundle", fixture)

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["facts"][1]["value"] = 3
    with pytest.raises(DataContractError):
        validate_contract("reka-fact-bundle", fixture)


def test_model_bundle_runtime_matches_supported_python_range() -> None:
    fixture_path = REPOSITORY_ROOT / "contracts" / "fixtures" / "model-bundle.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["runtime"]["python_version"] = "3.13.12"
    validate_contract("model-bundle", fixture)

    fixture["runtime"]["python_version"] = "3.11.9"
    with pytest.raises(DataContractError):
        validate_contract("model-bundle", fixture)
