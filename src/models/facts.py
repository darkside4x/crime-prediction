"""Reka-safe aggregate fact bundle generation."""

from __future__ import annotations

from typing import Any

from .provenance import stable_hash


def _fact_id(tenant_id: str, model_version: str, semantic_key: str) -> str:
    return f"fact_{stable_hash(tenant_id, model_version, semantic_key, length=24)}"


def build_fact_bundle(
    *,
    tenant_id: str,
    model_version: str,
    data_version: str,
    data_as_of: str,
    generated_at: str,
    window_start: str,
    window_end: str,
    evaluation: dict[str, Any],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build facts from deterministic artifacts without exposing cell-level rows."""
    facts: list[dict[str, Any]] = []
    selected = evaluation["selection"]["selected_model"]
    for metric in evaluation["metrics"]:
        if metric["model"] != selected or metric["split"] != "test":
            continue
        key = f"test_metric:{metric['name']}"
        facts.append(
            {
                "fact_id": _fact_id(tenant_id, model_version, key),
                "kind": "metric",
                "label": f"Untouched-test {metric['name'].replace('_', ' ')}",
                "value": metric["value"],
                "unit": "probability" if metric["name"] in {"top_k_capture", "pr_auc", "brier_score"} else "score",
                "definition": metric["definition"],
                "source_artifact": "evaluation_report",
                "data_as_of": data_as_of,
                "model_version": model_version,
                "suppressed": False,
            }
        )
    selection = evaluation["selection"]
    facts.append(
        {
            "fact_id": _fact_id(tenant_id, model_version, "selection:relative_gain"),
            "kind": "comparison",
            "label": "Validation gain versus historical-rate baseline",
            "value": selection["observed_relative_gain"],
            "unit": "percentage",
            "definition": "Relative validation improvement in the configured primary metric; model selection never uses the test block.",
            "source_artifact": "evaluation_report",
            "data_as_of": data_as_of,
            "model_version": model_version,
            "suppressed": False,
        }
    )
    visible = [row for row in predictions if not row["suppressed"]]
    prediction_summaries = (
        (
            "predictions:visible_rows",
            "Published aggregate prediction rows",
            len(visible),
            "count",
            "Number of aggregate prediction rows meeting the configured support threshold.",
            False,
        ),
        (
            "predictions:mean_risk",
            "Mean model-implied risk across published aggregates",
            (sum(row["risk"] for row in visible) / len(visible)) if visible else None,
            "probability",
            "Arithmetic mean of already-computed model-implied risk over published aggregate rows; this is not a new risk score.",
            not visible,
        ),
    )
    for key, label, value, unit, definition, suppressed in prediction_summaries:
        facts.append(
            {
                "fact_id": _fact_id(tenant_id, model_version, key),
                "kind": "prediction_summary",
                "label": label,
                "value": value,
                "unit": unit,
                "definition": definition,
                "source_artifact": "predictions",
                "data_as_of": data_as_of,
                "model_version": model_version,
                "suppressed": suppressed,
            }
        )
    facts.append(
        {
            "fact_id": _fact_id(tenant_id, model_version, "freshness:data_as_of"),
            "kind": "freshness",
            "label": "Model input data timestamp",
            "value": data_as_of,
            "unit": "timestamp",
            "definition": "Latest aggregate input timestamp declared by the tenant-scoped feature manifest.",
            "source_artifact": "predictions",
            "data_as_of": data_as_of,
            "model_version": model_version,
            "suppressed": False,
        }
    )
    bundle_key = stable_hash(tenant_id, model_version, data_version, window_start, window_end, length=24)
    return {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "bundle_id": f"factbundle_{bundle_key}",
        "bundle_version": "1.0.0",
        "model_version": model_version,
        "data_version": data_version,
        "data_as_of": data_as_of,
        "generated_at": generated_at,
        "scope": {
            "aggregation": "model_evaluation",
            "window_start": window_start,
            "window_end": window_end,
            "category": "all",
        },
        "facts": facts,
        "limitations": list(evaluation["limitations"]),
    }
