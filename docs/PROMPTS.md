# Team Work Prompts

These prompts implement the current ownership plan. All contributors must first read `AGENTS.md`, `docs/PHASE1_CONTRACTS.md`, `docs/ARCHITECTURE.md`, and `docs/TEAM_PLAN.md`.

## Person 1 — Video/data/platform backend

```text
Own src/data, src/features, Reka Vision orchestration, video/edge workers, migrations, storage/broker adapters, and focused tests. Use the server-injected REKA_API_KEY through a RekaVisionProvider to upload, index, analyze, and delete approved video; use a deterministic fake provider only in offline tests. Persist tenant-scoped opaque Reka video-ID mappings and validate Reka output into candidate-detection. Raw media, Reka IDs/URLs, evidence, coordinates, credentials, and event IDs remain restricted. Promotion requires one immutable confirmed review and must be idempotent. Generate measured coverage and unlabelled future rows. Test Reka failure/quota/injection/deletion, key leakage, expiration, future leakage, and cross-tenant denial.
```

## Person 2 — Forecasting/API/auth backend

```text
Own src/models, src/api, authentication, Reka provider injection, forecast orchestration, monitoring, and focused tests. Load one server-only REKA_API_KEY for Reka Vision and Reka Chat; never expose it through OpenAPI or browser responses. Derive tenant context server-side and enforce the roles in PHASE1_CONTRACTS. Proxy authorized video operations to Person 1's Reka service. Consume forecast-feature-row and return forecast; Reka never calculates risk. Refit after selection, calibrate on validation-only outputs, use temporal uncertainty, suppress with null values, and record all versions. Test Reka access/quota/timeout and key leakage alongside future-window, tenant, role, calibration, suppression, and fallback tests.
```

## Person 3 — Frontend

```text
Own src/web and frontend/browser tests. Use generated OpenAPI types and committed fixtures; do not create private contract copies. Upload only to FastAPI—never call Reka directly or include REKA_API_KEY in Vite configuration. Build tenant selection, recorded upload, Reka indexing/analysis status, live source setup, candidate review, coverage health, and H3 forecast map. Distinguish Reka-proposed candidates, confirmed aggregate incidents, and local future forecasts. Respect roles, suppression, uncertainty, freshness, provenance, limitations, accessibility, and reduced motion. Never expose Reka IDs/URLs, secret refs, coordinates, identities, or enforcement recommendations.
```

## Research review

```text
Read approved dataset documentation and every PDF under papers/. Record geography, time range, spatial unit, label process, licensing, features, model, chronological evaluation, baselines, reproducibility, limitations, and reporting/enforcement bias in docs/PAPER_MATRIX.md. Do not transfer published scores to this product without a comparable local evaluation.
```
