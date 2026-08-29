"""Server-side Reka gateway with a deterministic fake provider.

The gateway never sends raw events, coordinates, or cross-tenant context.
Every response is validated against contracts/schemas/reka-insight.schema.json;
invalid or uncited output degrades to the deterministic fallback.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .demo_data import DATA_AS_OF, DATA_VERSION, MODEL_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
_INSIGHT_SCHEMA = json.loads(
    (REPO_ROOT / "contracts" / "schemas" / "reka-insight.schema.json").read_text()
)
_insight_validator = Draft202012Validator(_INSIGHT_SCHEMA)

PROMPT_VERSION = "1.0.0"

_INJECTION_PATTERNS = [
    r"ignore (all|previous|above)", r"system prompt", r"reveal.*(key|secret|token)",
    r"disregard", r"you are now", r"raw coordinates", r"exact address",
]

_PROHIBITED_PATTERNS = [
    r"who (will|is going to) commit", r"suspect", r"which person",
    r"patrol", r"arrest", r"detain", r"individual",
]


def screen_question(question: str) -> str | None:
    """Return a refusal code when the question is unsafe, else None."""
    q = question.lower()
    for pattern in _INJECTION_PATTERNS + _PROHIBITED_PATTERNS:
        if re.search(pattern, q):
            return "unsafe_request"
    return None


def load_fact_bundle(tenant_id: str) -> dict[str, Any]:
    """Aggregate facts only. Fixture bundle rewritten to the caller's tenant."""
    bundle = json.loads(
        (REPO_ROOT / "contracts" / "fixtures" / "reka-fact-bundle.json").read_text()
    )
    bundle["tenant_id"] = tenant_id
    return bundle


class RekaProvider(Protocol):
    def complete(self, question: str, facts: dict[str, Any]) -> dict[str, Any]: ...


class FakeRekaProvider:
    """Deterministic provider used for tests and Reka-less demos."""

    def complete(self, question: str, facts: dict[str, Any]) -> dict[str, Any]:
        cited = [f for f in facts["facts"] if not f.get("suppressed")]
        if not cited:
            raise RuntimeError("no citable facts")
        fact = cited[0]
        answer = (
            f"{fact['label']} was {fact['value']} ({fact['definition']}) "
            "This is a forecast evaluation result, not a causal claim."
        )
        return {
            "answer": answer,
            "claims": [{"text": f"{fact['label']} was {fact['value']}.",
                        "fact_ids": [fact["fact_id"]]}],
            "limitations": facts.get("limitations", []),
        }


class BrokenProvider:
    """Returns uncited/malformed output — used to test fail-safe behaviour."""

    def complete(self, question: str, facts: dict[str, Any]) -> dict[str, Any]:
        return {"answer": "Crime will definitely rise 400% because of the moon.",
                "claims": [{"text": "made up", "fact_ids": ["fact_unknown"]}],
                "limitations": []}


def _bind_citations(raw: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    known = {f["fact_id"] for f in facts["facts"] if not f.get("suppressed")}
    claims = [c for c in raw.get("claims", []) if set(c.get("fact_ids", [])) <= known and c.get("fact_ids")]
    if not claims:
        raise ValueError("no grounded claims")
    return {**raw, "claims": claims}


def answer_question(
    tenant_id: str,
    question: str,
    provider: RekaProvider,
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    base = {
        "schema_version": "2.0.0",
        "tenant_id": tenant_id,
        "request_id": request_id,
        "data_as_of": DATA_AS_OF,
        "data_version": DATA_VERSION,
        "model_version": MODEL_VERSION,
        "reka_model": "fake-reka",
        "prompt_version": PROMPT_VERSION,
    }

    refusal = screen_question(question)
    if refusal:
        return {**base,
                "answer": "This assistant only answers aggregate, area-level questions grounded in published facts.",
                "claims": [], "limitations": [], "refusal_code": refusal}

    facts = load_fact_bundle(tenant_id)
    try:
        raw = provider.complete(question, facts)
        raw = _bind_citations(raw, facts)
        insight = {**base, **raw, "refusal_code": "not_applicable"}
        errors = list(_insight_validator.iter_errors(insight))
        if errors:
            raise ValueError(f"schema validation failed: {errors[0].message}")
        return insight
    except Exception:
        # Deterministic fallback: underlying metrics, clearly not AI text.
        return {**base,
                "answer": "AI explanation unavailable. Deterministic facts: "
                          + "; ".join(f"{f['label']}: {f['value']}" for f in facts["facts"] if not f.get("suppressed")),
                "claims": [], "limitations": facts.get("limitations", []),
                "refusal_code": "provider_unavailable"}
