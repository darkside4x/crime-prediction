# AGENTS.md

## Mission

Build a reproducible hackathon prototype that forecasts aggregate H3-cell incident risk for a future time window. Never build individual criminality scores, suspect identification, facial recognition, victim-address views, or automated enforcement recommendations.

## Required reading

Before changing code, read:

1. `docs/ARCHITECTURE.md`
2. `docs/TEAM_PLAN.md`
3. `docs/PAPER_MATRIX.md`
4. the relevant prompt in `docs/PROMPTS.md`

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
- The API contract in `docs/ARCHITECTURE.md` is the integration boundary.
- Synthetic fixtures are updated before consumers when a schema changes.
- Motion must communicate state, remain interruptible, and respect `prefers-reduced-motion`.

## Ownership

- Person 1: `src/data/`, `src/features/`, their tests, data schemas/config.
- Person 2: `src/models/`, model tests/config, prediction artifacts/model card.
- Person 3: `src/api/`, `src/web/`, containers, end-to-end tests, demo docs.
- Shared: root configuration, architecture, schemas, and safety text require review by another teammate.

Do not edit another owner's area to work around a broken contract. Document the mismatch and make the smallest shared-contract proposal.

## Engineering rules

- Prefer a clear baseline over an unverified complex model.
- Pin direct dependencies and keep the clean-start path documented.
- Keep secrets and large/raw datasets out of Git.
- Store connector credentials through secret references; never include credentials in source definitions, events, logs, or fixtures.
- Add tests for bug fixes and for every time/leakage-sensitive transform.
- Use deterministic seeds where supported; record data, code, config, and model versions.
- Avoid network calls in tests. Use tiny synthetic fixtures.
- Return typed, structured errors; do not silently drop malformed input.
- Test cross-tenant access denial and tenant-scoped cache/artifact keys.
- Do not claim causality from predictive features or explanations.

## Definition of done

A change is done only when its focused tests pass, its interface/schema is documented, generated artifacts include provenance, no raw sensitive fields leak, and the author reports the exact verification command. The complete demo is done when a clean checkout can start it, the historical baseline comparison is visible, one end-to-end map flow passes, and limitations are visible in the UI.

## Agent response format

At the start: state assumptions, owned paths, and acceptance criteria. At the end: state outcome, files changed, tests run/results, contract changes, and remaining risks. Never fabricate test results, paper findings, or metrics.
