# Aggregate Incident Forecasting Product

This repository is building a tenant-isolated product that accepts recorded footage or a tenant-owned live camera, creates human-reviewable candidate safety incidents, and forecasts future **aggregate incident volume** by H3 cell and time window.

It does not identify people, decide guilt, predict individual behaviour, expose exact victim locations, or recommend enforcement.

## Start here

1. [Phase 1 contract freeze](docs/PHASE1_CONTRACTS.md) — canonical team integration guide
2. [JSON Schema contract index](contracts/README.md) — authoritative machine-readable boundaries
3. [Architecture](docs/ARCHITECTURE.md) — target system and privacy boundaries
4. [Team plan](docs/TEAM_PLAN.md) — one frontend and two backend owners
5. [Research evidence matrix](docs/PAPER_MATRIX.md) — dataset/model evidence status
6. [Reka boundary](docs/REKA_AI.md) — deferred language interface, outside numeric prediction

Repository instructions are in [AGENTS.md](AGENTS.md).

## Current implementation status

Implemented today:

- versioned JSONL incident-event replay;
- tenant validation, idempotency, quarantine, and checkpoints;
- restricted coordinate-to-H3 aggregation;
- point-in-time historical feature generation;
- historical-rate, previous-period, regularized Poisson, and LightGBM candidates;
- chronological evaluation and versioned model artifacts.

Contracted but not yet implemented:

- MP4 upload and live RTSP/ONVIF ingestion;
- candidate detection and human review;
- measured source coverage;
- future feature snapshots and operational inference;
- FastAPI authentication/authorization endpoints;
- React/MapLibre product UI;
- Postgres/RLS, durable workers, monitoring, and drift checks.

Synthetic fixtures are interface examples, not real crime patterns or measured model performance.

## First end-to-end milestone

```text
recorded MP4
  -> candidate detections
  -> reviewer confirms/rejects
  -> confirmed aggregate incident history
  -> future six-hour H3 features
  -> operational forecast
  -> authenticated map
```

The forecasting model requires a separately approved historical incident dataset. One uploaded video is not sufficient training history.
