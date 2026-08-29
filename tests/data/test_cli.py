from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from src.data.cli import main
from src.models.config import ModelConfig
from src.models.data import load_rows, validate_rows


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
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_payload["feature_schema_version"] == "2.0.0"
    assert manifest_payload["artifact"]["sha256"] == result["feature_parquet_sha256"]
    assert len(validate_rows(load_rows(output), ModelConfig(enable_lightgbm=False))) == 40

    assert main(arguments) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["ingestion_run"]["accepted_count"] == 0
    assert resumed["ingestion_run"]["checkpoint"] == 4
    assert resumed["row_count"] == 40


def test_location_ref_cannot_escape_source_directory(tmp_path: Path) -> None:
    source = json.loads((FIXTURES / "source.json").read_text(encoding="utf-8"))
    source["config"]["location_ref"] = "../outside.jsonl"
    source_path = tmp_path / "source" / "source.json"
    source_path.parent.mkdir()
    source_path.write_text(json.dumps(source), encoding="utf-8")

    arguments = [
        "replay",
        "--source-definition",
        str(source_path),
        "--state-db",
        str(tmp_path / "state.sqlite"),
        "--output",
        str(tmp_path / "features.parquet"),
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--domain-cells",
        str(FIXTURES / "domain-cells.json"),
        "--start",
        "2026-08-01T00:00:00Z",
        "--end",
        "2026-08-02T00:00:00Z",
    ]

    try:
        main(arguments)
    except ValueError as exc:
        assert "must stay within" in str(exc)
    else:
        raise AssertionError("escaping location_ref was accepted")
