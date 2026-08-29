"""Deterministic fixture-backed demo predictions.

Generates tenant-scoped H3 risk grids that satisfy the prediction contract
so the dashboard works before (and without) real model output. When a real
prediction Parquet exists under artifacts/, it can replace this module
behind the same functions.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import h3

from .tenancy import DEMO_TENANT_ONE, DEMO_TENANT_TWO

MODEL_VERSION = "demo-one-historical-rate-v1"
DATA_VERSION = "features-demo-one-20260830-v1"
DATA_AS_OF = "2026-08-29T23:59:59Z"
H3_RESOLUTION = 8
GRID_RADIUS = 7  # k-ring around the tenant centre
WINDOW_HOURS = 6
N_WINDOWS = 4
SUPPRESSION_THRESHOLD = 0.04

CATEGORIES = ["all", "property", "violence", "public_order", "traffic_safety"]

# Tenant centres are deliberately far apart so cross-tenant leakage is obvious.
TENANT_CENTRES = {
    DEMO_TENANT_ONE: (12.9716, 77.5946),   # Bengaluru
    DEMO_TENANT_TWO: (13.0827, 80.2707),   # Chennai
}

_BASE_WINDOW = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)


def windows() -> list[dict[str, str]]:
    out = []
    for i in range(N_WINDOWS):
        start = _BASE_WINDOW + timedelta(hours=WINDOW_HOURS * i)
        end = start + timedelta(hours=WINDOW_HOURS)
        out.append({
            "window_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return out


def _unit_hash(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:12], 16) / float(16 ** 12)


def _risk_band(risk: float) -> str:
    if risk >= 0.66:
        return "elevated"
    if risk >= 0.33:
        return "moderate"
    return "typical"


_DRIVER_POOL = [
    "recent_7d_count", "rolling_7_mean", "lag_1_count", "neighbor_lag_1",
    "hour_of_day_cyclical", "day_of_week_cyclical", "recent_trend", "coverage_indicator",
]


@lru_cache(maxsize=None)
def tenant_cells(tenant_id: str) -> list[str]:
    lat, lng = TENANT_CENTRES[tenant_id]
    centre = h3.latlng_to_cell(lat, lng, H3_RESOLUTION)
    return sorted(h3.grid_disk(centre, GRID_RADIUS))


def prediction_for(tenant_id: str, cell_id: str, window_start: str, category: str) -> dict[str, Any]:
    u = _unit_hash(tenant_id, cell_id, window_start, category)
    # Shape the surface: a few hot pockets, mostly quiet cells.
    risk = round(min(0.97, max(0.02, u ** 1.7 * 1.25)), 3)
    suppressed = u < SUPPRESSION_THRESHOLD
    expected = round(risk * 1.4 * (0.6 + _unit_hash("count", cell_id, category)), 3)
    spread = 0.18 + 0.4 * _unit_hash("spread", cell_id, window_start)
    d1 = _DRIVER_POOL[int(u * 1e6) % len(_DRIVER_POOL)]
    d2 = _DRIVER_POOL[(int(u * 1e6) // 7) % len(_DRIVER_POOL)]
    if d2 == d1:
        d2 = _DRIVER_POOL[(_DRIVER_POOL.index(d1) + 3) % len(_DRIVER_POOL)]
    window_end = (
        datetime.strptime(window_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        + timedelta(hours=WINDOW_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, Any] = {
        "schema_version": "2.0.0",
        "tenant_id": tenant_id,
        "cell_id": cell_id,
        "window_start": window_start,
        "window_end": window_end,
        "category": category,
        "model_version": MODEL_VERSION,
        "data_version": DATA_VERSION,
        "data_as_of": DATA_AS_OF,
        "suppressed": suppressed,
    }
    if suppressed:
        return body
    body.update({
        "risk": risk,
        "risk_band": _risk_band(risk),
        "expected_count": expected,
        "uncertainty": {
            "lower": round(max(0.0, risk - spread * risk), 3),
            "upper": round(min(1.0, risk + spread * (1 - risk)), 3),
        },
        "drivers": [
            {"feature": d1, "direction": "higher"},
            {"feature": d2, "direction": "lower" if u > 0.5 else "higher"},
        ],
    })
    return body


def risk_feature_collection(tenant_id: str, window_start: str, category: str) -> dict[str, Any]:
    """GeoJSON for the map: H3 boundaries only — no raw coordinates or events."""
    features = []
    for cell_id in tenant_cells(tenant_id):
        pred = prediction_for(tenant_id, cell_id, window_start, category)
        boundary = h3.cell_to_boundary(cell_id)  # ((lat, lng), ...)
        ring = [[lng, lat] for lat, lng in boundary]
        ring.append(ring[0])
        props = {k: v for k, v in pred.items() if k not in {"schema_version", "tenant_id"}}
        features.append({
            "type": "Feature",
            "id": cell_id,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": props,
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "model_version": MODEL_VERSION,
        "data_version": DATA_VERSION,
        "data_as_of": DATA_AS_OF,
    }


def recent_trend(tenant_id: str, cell_id: str, category: str) -> list[dict[str, Any]]:
    """Deterministic recent aggregate counts for the cell-details panel."""
    out = []
    for i in range(14, 0, -1):
        day = _BASE_WINDOW - timedelta(days=i)
        u = _unit_hash("trend", tenant_id, cell_id, category, day.isoformat())
        out.append({"date": day.strftime("%Y-%m-%d"), "count": int(u * 6)})
    return out


def sources_for(tenant_id: str) -> list[dict[str, Any]]:
    suffix = "one" if tenant_id == DEMO_TENANT_ONE else "two"
    return [{
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "source_id": f"10000000-0000-4000-8000-00000000000{1 if suffix == 'one' else 2}",
        "name": f"Synthetic recorded replay ({suffix})",
        "kind": "recorded_replay",
        "status": "active",
        "freshness": {
            "last_accepted_event_at": DATA_AS_OF,
            "last_received_at": DATA_AS_OF,
            "lag_seconds": 42,
            "rejected_count": 3,
        },
    }]
