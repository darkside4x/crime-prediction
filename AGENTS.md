# AGENTS.md

## Mission

Build a reproducible hackathon prototype that forecasts aggregate H3-cell incident risk for a future time window. Never build individual criminality scores, suspect identification, facial recognition, victim-address views, or automated enforcement recommendations.

## Required reading

Before changing code, read:

1. `docs/PHASE1_CONTRACTS.md`
2. `docs/ARCHITECTURE.md`
3. `docs/TEAM_PLAN.md`
4. `docs/PAPER_MATRIX.md`
5. `docs/REKA_AI.md` when touching AI, API, facts, onboarding, or frontend copilot behavior
6. the relevant prompt in `docs/PROMPTS.md`

## Non-negotiable contracts

- Prediction key: `(tenant_id, cell_id, interval_start, category)`.
- `tenant_id` is resolved from authenticated server-side context, never trusted from a client-supplied query parameter.
- Every source, ingestion offset, feature row, artifact, model version, and prediction is tenant-scoped.
- Recorded replay and live adapters emit the same versioned event envelope in `contracts/schemas/incident-event.schema.json`.
- All timestamps are UTC at storage/API boundaries.
- Features at time `t` may use only information available before `t`.
- Evaluation splits are chronological; random splits are prohibited.
- Raw coordinates and event identifiers never leave the ingestion boundary.
- Public predictions are aggregated and low-count outputs are suppressed.
- Candidate detections are unconfirmed, reviewer-only records. Only an immutable confirmed review may promote an incident event.
- Historical `feature-row` records contain labels; future `forecast-feature-row` records never contain `event_count`.
- Public operational responses use `forecast.schema.json`; suppressed estimates are null and must never be presented as zero risk.
- The endpoint, role, and payload rules in `docs/PHASE1_CONTRACTS.md` plus `contracts/schemas/` are the integration boundary.
- Synthetic fixtures are updated before consumers when a schema changes.
- Motion must communicate state, remain interruptible, and respect `prefers-reduced-motion`.
- The backend uses one server-side `REKA_API_KEY` for Reka Vision and Reka Chat; no separate browser or Vision key is permitted.
- Reka Vision may manage and analyze tenant-owned, lawfully obtained, explicitly approved video. It may propose candidate incidents but never confirm a crime or bypass human review.
- Reka must never receive exact camera coordinates, incident/event identifiers, credentials, secret references, identity watchlists, facial embeddings, protected-attribute labels, or cross-tenant context.
- Reka may map schemas, summarize validated aggregate facts, and orchestrate allowlisted read-only tools; it must never calculate or modify future risk scores.
- Every Reka output crossing a system boundary must validate against a versioned schema and record the relevant video/model/configuration, prompt, data, and forecast model versions.

## Ownership

- Person 1 — video/data/platform backend: `src/data/`, `src/features/`, video/edge workers, storage/broker adapters, migrations, and their tests.
- Person 2 — forecasting/API/auth backend: `src/models/`, `src/api/`, authentication/authorization, forecast orchestration, monitoring, and their tests.
- Person 3 — frontend: `src/web/`, frontend fixtures, accessibility tests, and browser end-to-end tests.
- Shared: root configuration, architecture, schemas, and safety text require review by another teammate.

Do not edit another owner's area to work around a broken contract. Document the mismatch and make the smallest shared-contract proposal.

## Engineering rules

- Prefer a clear baseline over an unverified complex model.
- Pin direct dependencies and keep the clean-start path documented.
- Keep secrets and large/raw datasets out of Git.
- Store connector credentials through secret references; never include credentials in source definitions, events, logs, or fixtures.
- Keep `REKA_API_KEY` server-side, use bounded timeouts/retries, tenant quotas, Reka video-ID tenant mappings, and monitored remote deletion. Provide a deterministic or manual-review fallback for every critical workflow.
- Add tests for bug fixes and for every time/leakage-sensitive transform.
- Use deterministic seeds where supported; record data, code, config, and model versions.
- Avoid network calls in tests. Use tiny synthetic fixtures.
- Return typed, structured errors; do not silently drop malformed input.
- Test cross-tenant access denial and tenant-scoped cache/artifact keys.
- Mock Reka in tests; test video upload/index/delete failures, prompt-injection strings, invalid structured output, cross-tenant video IDs, unavailable APIs, key leakage, and refusal behavior.
- Do not claim causality from predictive features or explanations.

## Definition of done

A change is done only when its focused tests pass, its interface/schema is documented, generated artifacts include provenance, no raw sensitive fields leak, and the author reports the exact verification command. The complete demo is done when a clean checkout can start it, the historical baseline comparison is visible, one end-to-end map flow passes, and limitations are visible in the UI.

## Agent response format

At the start: state assumptions, owned paths, and acceptance criteria. At the end: state outcome, files changed, tests run/results, contract changes, and remaining risks. Never fabricate test results, paper findings, or metrics.
