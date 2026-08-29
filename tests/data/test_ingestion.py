from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.data.adapters import RecordedReplayAdapter
from src.data.category_map import CategoryMap
from src.data.errors import IngestionError
from src.data.service import IngestionService
from src.data.source import SourceDefinition
from src.data.store import IngestionStore


TENANT_A = "00000000-0000-4000-8000-000000000001"
TENANT_B = "00000000-0000-4000-8000-000000000002"
SOURCE_A = "00000000-0000-4000-8000-000000000101"
SOURCE_B = "00000000-0000-4000-8000-000000000202"
ROOT = Path(__file__).resolve().parents[2]
CATEGORY_MAP = ROOT / "configs" / "data" / "category-map.json"


def source_definition(tenant_id: str = TENANT_A, source_id: str = SOURCE_A) -> SourceDefinition:
    return SourceDefinition.from_dict(
        {
            "schema_version": "1.0.0",
            "tenant_id": tenant_id,
            "source_id": source_id,
            "name": "Test replay",
            "kind": "recorded_replay",
            "status": "active",
            "config": {
                "format": "jsonl",
                "location_ref": "events.jsonl",
                "replay_speed": 1,
                "loop": False,
                "timezone": "UTC",
            },
            "created_at": "2026-08-01T00:00:00Z",
        }
    )


def event(
    event_id: str,
    *,
    tenant_id: str = TENANT_A,
    source_id: str = SOURCE_A,
    occurred_at: str = "2026-08-01T05:30:00+05:30",
    received_at: str = "2026-08-01T05:31:00+05:30",
    category: str = "Theft",
    latitude: float = 12.9716,
    longitude: float = 77.5946,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "source_id": source_id,
        "external_event_id": event_id,
        "occurred_at": occurred_at,
        "received_at": received_at,
        "category": category,
        "location": {"latitude": latitude, "longitude": longitude},
        "attributes": {
            "reporting_channel": "fixture",
            "victim_name": "must be dropped at ingestion",
        },
    }


def write_jsonl(path: Path, values: list[dict[str, object] | str]) -> None:
    lines = [value if isinstance(value, str) else json.dumps(value) for value in values]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ingest(path: Path, store: IngestionStore, source: SourceDefinition) -> dict[str, object]:
    service = IngestionService(store, CategoryMap.from_file(CATEGORY_MAP))
    adapter = RecordedReplayAdapter(source, store, path)
    return asyncio.run(
        service.ingest_replay(adapter, source, authenticated_tenant_id=source.tenant_id)
    )


def test_replay_normalizes_deduplicates_quarantines_and_resumes(tmp_path: Path) -> None:
    replay = tmp_path / "events.jsonl"
    valid = event("event-1")
    invalid_coordinate = event("event-2", latitude=120)
    wrong_tenant = event("event-3", tenant_id=TENANT_B)
    write_jsonl(replay, [valid, valid, invalid_coordinate, wrong_tenant, "{not-json"])

    store = IngestionStore(tmp_path / "state.sqlite")
    source = source_definition()
    first_run = ingest(replay, store, source)

    assert first_run["status"] == "completed"
    assert first_run["checkpoint"] == 5
    assert first_run["accepted_count"] == 1
    assert first_run["duplicate_count"] == 1
    assert first_run["rejected_count"] == 3
    assert store.quarantine_count(TENANT_A, SOURCE_A) == 3

    stored = store.list_events(TENANT_A)
    assert len(stored) == 1
    assert stored[0]["occurred_at"] == "2026-08-01T00:00:00Z"
    assert stored[0]["received_at"] == "2026-08-01T00:01:00Z"
    assert stored[0]["category"] == "property"
    assert json.loads(stored[0]["attributes_json"]) == {"reporting_channel": "fixture"}

    second_run = ingest(replay, store, source)
    assert second_run["checkpoint"] == 5
    assert second_run["accepted_count"] == 0
    assert second_run["duplicate_count"] == 0
    assert second_run["rejected_count"] == 0

    with replay.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event("event-4", occurred_at="2026-08-01T06:00:00Z", received_at="2026-08-01T06:01:00Z")) + "\n")
    resumed_run = ingest(replay, store, source)
    assert resumed_run["checkpoint"] == 6
    assert resumed_run["accepted_count"] == 1
    assert store.event_count(TENANT_A) == 2


def test_authenticated_tenant_cannot_run_another_tenants_source(tmp_path: Path) -> None:
    replay = tmp_path / "events.jsonl"
    write_jsonl(replay, [event("event-1")])
    store = IngestionStore(tmp_path / "state.sqlite")
    source = source_definition()
    service = IngestionService(store, CategoryMap.from_file(CATEGORY_MAP))
    adapter = RecordedReplayAdapter(source, store, replay)

    with pytest.raises(IngestionError, match="authenticated tenant"):
        asyncio.run(service.ingest_replay(adapter, source, authenticated_tenant_id=TENANT_B))
    assert store.event_count(TENANT_A) == 0
    assert store.event_count(TENANT_B) == 0


def test_tenant_queries_never_mix_events(tmp_path: Path) -> None:
    store = IngestionStore(tmp_path / "state.sqlite")
    replay_a = tmp_path / "a.jsonl"
    replay_b = tmp_path / "b.jsonl"
    source_a = source_definition(TENANT_A, SOURCE_A)
    source_b = source_definition(TENANT_B, SOURCE_B)
    write_jsonl(replay_a, [event("a-1", tenant_id=TENANT_A, source_id=SOURCE_A)])
    write_jsonl(replay_b, [event("b-1", tenant_id=TENANT_B, source_id=SOURCE_B)])

    ingest(replay_a, store, source_a)
    ingest(replay_b, store, source_b)

    assert {row["external_event_id"] for row in store.list_events(TENANT_A)} == {"a-1"}
    assert {row["external_event_id"] for row in store.list_events(TENANT_B)} == {"b-1"}


def test_same_event_id_with_changed_content_is_quarantined_as_conflict(tmp_path: Path) -> None:
    replay = tmp_path / "events.jsonl"
    original = event("event-1")
    changed = event("event-1", longitude=77.7)
    write_jsonl(replay, [original, changed])
    store = IngestionStore(tmp_path / "state.sqlite")

    run = ingest(replay, store, source_definition())

    assert run["accepted_count"] == 1
    assert run["duplicate_count"] == 0
    assert run["rejected_count"] == 1
    assert run["last_error_code"] == "idempotency_conflict"
    assert store.quarantine_count(TENANT_A, SOURCE_A) == 1
