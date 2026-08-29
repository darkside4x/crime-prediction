# Team Work Prompts

These prompts implement the current ownership plan. All contributors must first read `AGENTS.md`, `docs/PHASE1_CONTRACTS.md`, `docs/ARCHITECTURE.md`, and `docs/TEAM_PLAN.md`.

## Person 1 — Video/data/platform backend

```text
Own src/data, src/features, video and edge workers, migrations, storage/broker adapters, and focused tests. Implement only against the versioned camera-source, video-asset, candidate-detection, candidate-review, coverage-snapshot, incident-event, and forecast-feature-row contracts. Raw media, evidence, exact locations, credentials, and event IDs remain restricted. Promotion requires one immutable confirmed review and must be idempotent. Generate measured coverage and unlabelled future feature rows with data_as_of strictly before interval_start. Test retries, expiration, malformed media, future leakage, and cross-tenant denial.
```

## Person 2 — Forecasting/API/auth backend

```text
Own src/models, src/api, authentication, forecast orchestration, monitoring, and focused tests. Derive tenant context server-side and enforce the roles in PHASE1_CONTRACTS. Consume forecast-feature-row and return forecast; never use the legacy evaluation prediction as a public operational response. Refit after selection, calibrate on validation-only outputs, use temporal uncertainty, suppress with null values, and record all versions. Return typed api-error payloads. Test future-window semantics, cross-tenant denial, role denial, calibration provenance, suppressed output, and unavailable-model fallback.
```

## Person 3 — Frontend

```text
Own src/web and frontend/browser tests. Use generated OpenAPI types and the committed fixtures; do not create private contract copies. Build tenant selection, recorded upload, live source setup, processing status, candidate review, coverage health, and H3 forecast map. Visually and textually distinguish unconfirmed candidate detections, confirmed aggregate incidents, and future forecasts. Respect role restrictions, suppression, uncertainty, freshness, provenance, limitations, accessibility, and reduced motion. Never expose secret references, raw coordinates, identities, or enforcement recommendations.
```

## Research review

```text
Read approved dataset documentation and every PDF under papers/. Record geography, time range, spatial unit, label process, licensing, features, model, chronological evaluation, baselines, reproducibility, limitations, and reporting/enforcement bias in docs/PAPER_MATRIX.md. Do not transfer published scores to this product without a comparable local evaluation.
```
