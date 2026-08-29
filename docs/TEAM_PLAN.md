# Three-Person Team Plan

The work is divided by stable interfaces so all three people can work in parallel after a short contract-freeze session.

## Person 1 - Data and geospatial pipeline

Owns `src/data/`, `src/features/`, data schemas, and leakage tests.

Deliverables:

- dataset inventory and data dictionary;
- deterministic ingestion and category mapping;
- canonical replay/live event envelope, tenant-aware source registry, and recorded-stream replay adapter;
- ingestion idempotency, checkpoints, quarantine/dead-letter output, and source health metrics;
- coordinate/privacy checks and H3 aggregation;
- time-complete cell-by-interval table;
- lag, rolling, calendar, neighbor, and coverage features;
- Parquet feature dataset plus a small synthetic fixture;
- tests proving no future timestamp contributes to a feature.
- a redacted source-profile contract for Reka-assisted schema mapping; raw samples remain local and require explicit allowlisting.

Acceptance: one command replays a recorded source into a versioned tenant-scoped feature table; duplicates, restarts, invalid coordinates, missing intervals, timezones, and cross-tenant mixing are tested.

## Person 2 - Modeling and evaluation

Owns `src/models/`, experiment configuration, exported predictions, and the model card.

Deliverables:

- historical-rate and Poisson baselines;
- chronological split and walk-forward evaluation;
- LightGBM count/risk model;
- calibration and top-k ranking analysis;
- geographic/time/category error slices;
- versioned model bundle and precomputed prediction Parquet;
- tenant-scoped training/evaluation manifests and artifact paths;
- concise model card with limitations.
- deterministic aggregate fact bundles used by the Reka explainer, with stable `fact_id` values and no raw records.

Acceptance: a single evaluation command reproduces the comparison table on an untouched test window; the selected model must beat the naive baseline on the primary metric or the baseline is shipped honestly.

## Person 3 - API, dashboard, and integration

Owns `src/api/`, `src/web/`, Docker Compose, and the demo path.

Deliverables:

- FastAPI endpoints matching `docs/ARCHITECTURE.md`;
- authentication/tenant-context middleware, source management endpoints, and tenant isolation tests;
- typed React client generated from OpenAPI or equivalent shared types;
- React/TypeScript MapLibre risk layer, tenant/source status, time/category controls, legend, cell details, and limitations panel;
- Motion (`motion/react`) state transitions with reduced-motion support;
- server-side Reka gateway, structured-output validation, allowlisted read-only tools, tenant quotas, audit metadata, and fake provider for tests;
- source-mapping review UI and a grounded copilot panel that exposes citations/data freshness;
- fixture-backed UI before model output is ready;
- API tests and one end-to-end browser smoke test;
- reproducible demo command and short presentation script.

Acceptance: from a clean checkout, the core map works without Reka; with a valid server-side key, source mapping and the grounded copilot work. Invalid AI output, timeouts, prompt injection, and cross-tenant tool access fail safely.

## Shared checkpoints

| Checkpoint | Whole-team decision/output |
|---|---|
| Hour 1 | target, grid resolution, time window, taxonomy, tenant/event schemas, primary metric |
| 25% | synthetic feature and prediction fixtures exchanged |
| 50% | baseline report plus fixture-backed API/UI and fake-Reka copilot demo |
| 75% | real predictions and Reka integrated; safety and failure-state review |
| Final | clean-machine run, frozen artifacts, 3-minute demo rehearsal |

## Integration rules

- Each person works on a short-lived branch: `data/*`, `model/*`, or `app/*`.
- Merge small contract changes early; do not pass datasets through Git.
- Commit schemas, configs, tiny fixtures, and artifact manifests.
- Store large local artifacts under `artifacts/` and identify them with a checksum and generation command.
- Any interface change requires updating the synthetic fixture first.
- Every integration test uses at least two tenants and verifies that tenant A cannot observe tenant B data.
- All three review the final model card and limitations text.
- Person 1 reviews source-mapping inputs; Person 2 reviews AI fact bundles; Person 3 owns Reka transport/UI. No one person may silently expand the AI tool allowlist.
