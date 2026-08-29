"""Resumable JSONL recorded-stream replay adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from ..errors import IngestionError
from ..source import SourceDefinition
from ..store import IngestionStore
from .base import AdapterItem, SourceHealth


class RecordedReplayAdapter:
    def __init__(self, source: SourceDefinition, store: IngestionStore, input_path: Path) -> None:
        if source.kind != "recorded_replay":
            raise ValueError("RecordedReplayAdapter requires a recorded_replay source")
        if source.config.get("format") != "jsonl":
            raise ValueError("The initial replay adapter supports JSONL sources only")
        if source.config.get("loop", False):
            raise ValueError("Looping replay is disabled for deterministic feature generation")
        self.source = source
        self.store = store
        self.input_path = Path(input_path)

    async def validate_connection(self) -> SourceHealth:
        if not self.input_path.is_file():
            return SourceHealth(False, "Replay input does not exist")
        try:
            with self.input_path.open("r", encoding="utf-8") as handle:
                handle.read(1)
        except OSError:
            return SourceHealth(False, "Replay input is not readable")
        return SourceHealth(True, "Replay input is readable")

    async def read(self, checkpoint: str | int | None) -> AsyncIterator[AdapterItem]:
        start_after = int(checkpoint or 0)
        try:
            with self.input_path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if line_number <= start_after:
                        continue
                    stripped = raw_line.strip()
                    if not stripped:
                        yield AdapterItem(
                            checkpoint=line_number,
                            payload=None,
                            raw_value="",
                            error_code="empty_record",
                            safe_detail="Replay line is empty",
                        )
                        continue
                    try:
                        payload = json.loads(stripped)
                    except json.JSONDecodeError:
                        yield AdapterItem(
                            checkpoint=line_number,
                            payload=None,
                            raw_value=stripped,
                            error_code="malformed_json",
                            safe_detail="Replay line is not valid JSON",
                        )
                        continue
                    if not isinstance(payload, dict):
                        yield AdapterItem(
                            checkpoint=line_number,
                            payload=None,
                            raw_value=stripped,
                            error_code="record_not_object",
                            safe_detail="Replay record must be a JSON object",
                        )
                        continue
                    yield AdapterItem(checkpoint=line_number, payload=payload)
        except OSError as error:
            raise IngestionError("source_read_failed", "Replay input could not be read", retryable=True) from error

    async def commit(self, checkpoint: str | int) -> None:
        self.store.set_checkpoint(self.source.tenant_id, self.source.source_id, checkpoint)
