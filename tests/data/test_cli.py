from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from src.data.cli import main


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_one_command_replays_source_and_writes_features(tmp_path: Path, capsys) -> None:
    state_db = tmp_path / "state.sqlite"
    output = tmp_path / "features.parquet"
    manifest = tmp_path / "features.manifest.json"
    arguments = [
        "replay",
        "--source-definition",
        str(FIXTURES / "source.json"),
        "--state-db",
        str(state_db),
        "--output",
        str(output),
        "--manifest",
        str(manifest),
        "--domain-cells",
        str(FIXTURES / "domain-cells.json"),
        "--start",
        "2026-08-01T00:00:00Z",
        "--end",
        "2026-08-02T00:00:00Z",
        "--interval-hours",
        "6",
    ]

    assert main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ingestion_run"]["accepted_count"] == 4
    assert result["row_count"] == 40
    assert output.is_file()
    assert len(pl.read_parquet(output)) == 40

    assert main(arguments) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["ingestion_run"]["accepted_count"] == 0
    assert resumed["ingestion_run"]["checkpoint"] == 4
    assert resumed["row_count"] == 40
