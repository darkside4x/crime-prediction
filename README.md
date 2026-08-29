# Aggregate Incident Forecasting Product

This repository is building a tenant-isolated product that accepts recorded footage or a tenant-owned live camera, creates human-reviewable candidate safety incidents, and forecasts future **aggregate incident volume** by H3 cell and time window.

It does not identify people, decide guilt, predict individual behaviour, expose exact victim locations, or recommend enforcement.

## Start here

1. [Phase 1 contract freeze](docs/PHASE1_CONTRACTS.md) — canonical team integration guide
2. [JSON Schema contract index](contracts/README.md) — authoritative machine-readable boundaries
3. [Architecture](docs/ARCHITECTURE.md) — target system and privacy boundaries
4. [Team plan](docs/TEAM_PLAN.md) — one frontend and two backend owners
5. [Research evidence matrix](docs/PAPER_MATRIX.md) — dataset/model evidence status
6. [Reka AI and Vision](docs/REKA_AI.md) — managed video intelligence and grounded forecast explanations
7. [Reka platform research](docs/REKA_PLATFORM_RESEARCH.md) — current APIs, limits, AWS architecture, and product decisions
8. [Historical benchmark and real results](docs/EVALUATION.md) — lawful dataset decision, reproducible chronological evaluation, and limitations
9. [AWS VM deployment](deploy/aws-vm/README.md) — hardened production composition and scale controls

Repository instructions are in [AGENTS.md](AGENTS.md).

## Current implementation status

Implemented today:

- versioned JSONL incident-event replay;
- tenant validation, idempotency, quarantine, and checkpoints;
- restricted coordinate-to-H3 aggregation;
- point-in-time historical feature generation;
- historical-rate, previous-period, regularized Poisson, and LightGBM candidates;
- chronological evaluation and versioned model artifacts;
- authenticated FastAPI forecast/model/copilot endpoints with demo tenant isolation, typed errors, idempotency, and audit records;
- schema-valid operational historical-rate fallback with coverage/support suppression;
- live server-side Reka Chat explanations with strict grounding and deterministic failure fallback;
- PostgreSQL repositories with transaction-scoped tenant RLS;
- SQS-compatible leased video workers with retries, heartbeats, recovery and DLQ transfer;
- tenant-prefixed S3/KMS media storage, ClamAV scanning and retention deletion;
- secret-backed bounded HLS/RTSP/ONVIF segmentation with backpressure;
- measured source coverage propagated into future forecast rows;
- operation-specific SQS stage queues and a shared PostgreSQL rate limiter for horizontal replicas;
- a production FastAPI composition backed by Postgres/RLS, S3/KMS, SQS/DLQ, OIDC and durable audit/idempotency stores;
- non-root, read-only container defaults and a hardened same-origin reverse proxy;
- bounded allowlisted public-HLS capture into restricted `live_segment` MP4 assets;
- backend-proxied Reka Vision upload/index/candidate analysis with strict output validation;
- tenant-scoped candidate listing and immutable human confirmation/rejection;
- a fixture-backed React/MapLibre dashboard and Docker demo setup.

Contracted but not yet implemented:

- Reka Vision video search, tagging and highlight generation;
- future feature snapshot generation and an approved calibrated-model registry;
- recorded-video onboarding/review screens connected to the real APIs;
- provisioned AWS resources, real-camera certification, alerting, backup-restore drills and drift checks.

Synthetic fixtures are interface examples, not real crime patterns or measured model performance.

## First end-to-end milestone

```text
recorded MP4
  -> FastAPI -> Reka Vision upload/index/analysis
  -> validated candidate detections
  -> reviewer confirms/rejects
  -> confirmed aggregate incident history
  -> future six-hour H3 features
  -> operational forecast
  -> authenticated map
```

The forecasting model requires a separately approved historical incident dataset. One uploaded video is not sufficient training history.

The backend uses one secret named `REKA_API_KEY` for Reka Vision and Reka Chat. It must never be placed in frontend/Vite configuration or committed to Git. Reka handles managed video intelligence; the reproducible local model still calculates numeric future H3 forecasts.
