# Architecture

## 1. Product boundary

Build a decision-support prototype that estimates **where and when incident volume may be elevated**, at an aggregate grid-cell level. The safest useful hackathon framing is resource-awareness and public-safety planning, with a human interpreting the output. Do not expose individual scores, suspect lists, exact victim addresses, or prescriptive patrol allocation.

## 2. End-to-end design

```text
Recorded replay now / tenant-owned live adapters later
                         |
                         v
 Versioned envelope, source auth, idempotency, quarantine
                         |
                         v
 Tenant boundary + privacy filter + H3 aggregation
                         |
                         v
 Tenant x time-complete cell x interval feature table
                         |
             +-----------+-----------+
             |                       |
             v                       v
 Historical-rate baseline    LightGBM count/risk model
             |                       |
             +-----------+-----------+
                         v
       Walk-forward evaluation + calibration
                         |
                         v
       Versioned model bundle and model card
                         |
                         v
       Tenant-aware FastAPI application API
                         |
                         v
 React + TypeScript + MapLibre + Motion dashboard
```

### Deployment shape

For the recorded demo, run one API process and one background worker using DuckDB/Parquet plus a local durable inbox. The replay adapter reads JSONL/CSV at a configurable event rate and submits the canonical envelope to the same ingestion service used by live adapters.

For a post-demo deployment, replace the local inbox with a durable broker and Postgres/PostGIS, without changing event or prediction schemas. Webhook, Kafka, and MQTT connectors are adapters; they do not contain feature or modeling logic.

```text
Tenant source -> adapter -> durable inbox -> validator/idempotency -> raw restricted store
                                                    |
                                                    v
                                     aggregate/features/predictions
                                                    |
                                  tenant-authenticated API and UI
```

## 3. Canonical contracts

### Raw incident record

| Field | Type | Rule |
|---|---|---|
| `schema_version` | string | starts at `1.0.0`; adapters reject unsupported major versions |
| `tenant_id` | UUID | mandatory partition and authorization boundary |
| `source_id` | UUID | registered source owned by the same tenant |
| `external_event_id` | string | stable within source; used only for ingestion idempotency |
| `occurred_at` | UTC timestamp | retain source timezone separately if needed |
| `latitude`, `longitude` | float | never returned by the public API |
| `category` | string | mapped to a small documented taxonomy |
| `received_at` | UTC timestamp | adapter receipt time for freshness and operations |

The authoritative JSON Schemas are under `contracts/schemas/`. Ingestion performs schema validation, verifies that `source_id` belongs to the authenticated tenant, computes an idempotency key from `(tenant_id, source_id, external_event_id)`, and quarantines invalid events with a reason code. Raw coordinates are held only in the restricted ingestion store and are replaced by `cell_id` before downstream publication.

### Model row

Primary key: `(tenant_id, cell_id, interval_start, category)`.

Required fields include lagged counts (`1`, `2`, `7`, and `14` comparable intervals), rolling means, hour/day cyclical encodings, recent trend, neighboring-cell lag, and an exposure/coverage indicator. The label is the next-window count or `count > 0` depending on dataset density.

Every feature must be computable using data available strictly before `interval_start`. This rule is the main defense against leakage.

### Prediction response

```json
{
  "tenant_id": "00000000-0000-4000-8000-000000000001",
  "cell_id": "H3_CELL",
  "window_start": "2026-08-30T00:00:00Z",
  "window_end": "2026-08-30T06:00:00Z",
  "category": "all",
  "risk": 0.42,
  "risk_band": "elevated",
  "expected_count": 0.31,
  "uncertainty": {"lower": 0.16, "upper": 0.55},
  "drivers": [{"feature": "recent_7d_count", "direction": "higher"}],
  "model_version": "...",
  "data_as_of": "..."
}
```

`tenant_id` is present in internal responses and server logs for traceability, but it is resolved from the authenticated principal. It is never accepted as a client-controlled filter. Browser clients may select among tenants returned by `/v1/me/tenants`; changing selection replaces the authenticated tenant context.

## 4. Multi-tenant and streaming architecture

### Isolation model

- Start with a shared application and logically isolated tenant partitions.
- Require `tenant_id` in database keys, Parquet partitions, job payloads, caches, object paths, metrics labels, model registries, and audit records.
- Derive tenant context from authentication middleware and pass a non-optional `TenantContext` through services.
- Apply Postgres row-level security if Postgres is introduced; application filtering is not sufficient by itself.
- Use per-tenant encryption/object prefixes and quotas in a production evolution.
- Train one model per tenant for the prototype. Cross-tenant/federated learning is out of scope.

The demo can use two synthetic tenants to demonstrate isolation even if only one real recorded dataset is available.

### Source lifecycle

`draft -> validating -> active -> degraded -> paused -> archived`

A source definition contains transport configuration and a `secret_ref`, never credentials. The worker records checkpoint/offset, last accepted event time, last received time, lag, rejected count, and last error. Replays are resumable and idempotent.

### Adapter contract

Every adapter implements the conceptual interface:

```python
class EventSourceAdapter(Protocol):
    async def validate_connection(self) -> SourceHealth: ...
    async def read(self, checkpoint: Checkpoint | None) -> AsyncIterator[IncidentEvent]: ...
    async def commit(self, checkpoint: Checkpoint) -> None: ...
```

Initial adapter: recorded JSONL/CSV replay with speed, start time, end time, and loop controls. Future adapters: signed webhook, Kafka consumer, or MQTT subscriber. All normalize to `incident-event.schema.json` before entering the pipeline.

### Delivery semantics

- At-least-once ingestion plus idempotent processing.
- Unique event key: `(tenant_id, source_id, external_event_id)`.
- Per-source ordered checkpoints where the transport supports them.
- Bounded retry with exponential backoff; invalid payloads go directly to quarantine.
- Event time drives features; received time drives freshness and operational alerts.
- Late data inside a configured watermark recomputes affected aggregates; older data is retained for the next batch rebuild and does not silently rewrite published forecasts.

## 5. Modeling plan

Use models in this order:

1. Historical mean by cell, category, and comparable time bucket.
2. Regularized Poisson regression as an interpretable statistical baseline.
3. LightGBM Poisson/Tweedie objective for the demo candidate.

Only attempt a graph neural network, ConvLSTM, or transformer after the baseline pipeline is complete and the paper matrix shows a credible expected gain. These models add substantial implementation and leakage risk for a short hackathon.

For sparse data, predict counts and rank cells. If the judges require classification, derive `count > 0` and calibrate probabilities on a validation window. Never use random train/test splitting.

## 6. Evaluation

- Split chronologically: train, validation, then untouched test.
- Prefer expanding-window or rolling-origin validation.
- Compare against historical-rate and previous-period baselines.
- Count task: MAE, Poisson deviance, and top-k capture rate.
- Classification task: PR-AUC, Brier score, calibration curve, and top-k capture rate.
- Slice by category, geography, time-of-day, and reporting-coverage proxy.
- Report spatial displacement: whether errors concentrate in specific neighborhoods.
- Include bootstrap confidence intervals when time permits.

Accuracy alone is not sufficient. A map that merely reproduces historic reporting or enforcement intensity must be described as such.

## 7. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | common data/ML ecosystem |
| Packaging | `uv`, `pyproject.toml` | fast reproducible environments |
| Dataframes | Polars; Pandas only for library boundaries | fast and simple local workflow |
| Spatial | H3 + GeoPandas/Shapely | privacy-preserving aggregation and map geometry |
| Storage | Parquet + DuckDB | zero-infrastructure analytics for a hackathon |
| Demo ingestion | JSONL/CSV replay + local durable inbox | exercises streaming contracts without broker operations |
| Production evolution | Postgres/PostGIS + Redpanda/Kafka or managed equivalent | durable tenant-aware state and streams when scale requires it |
| Validation | Pandera or Pydantic | explicit data contracts |
| ML | scikit-learn + LightGBM | strong tabular baselines and fast iteration |
| Explainability | LightGBM contributions or SHAP on sampled rows | cell-level drivers without slowing the API |
| API | FastAPI + Pydantic | typed and self-documenting |
| UI | React, TypeScript, Vite, MapLibre GL, TanStack Query, Motion (`motion/react`) | typed interactive map with purposeful state transitions |
| Tests | Pytest + Vitest/Playwright smoke test | proportionate verification |
| Runtime | Docker Compose | reproducible demo |
| CI | GitHub Actions | free for public repositories and familiar |

Add Postgres/PostGIS and a broker only when live source durability, concurrent tenants, or dataset size requires them. The demo must not depend on these services.

### Frontend motion contract

- Use Motion for source connection/replay status, panel transitions, selection continuity, and non-blocking feedback.
- Prefer transform/opacity and short transitions; never animate the heatmap to imply false precision or urgency.
- Configure reduced motion globally and verify the main flow with `prefers-reduced-motion` enabled.
- Loading skeletons preserve layout; streaming updates do not steal focus or reset map position.
- Animations are interruptible and the application remains usable if JavaScript animation is unavailable.

## 8. API surface

- `GET /health`
- `GET /v1/me/tenants` - authorized tenant choices
- `GET /v1/metadata` - categories, valid windows, model/data versions
- `GET /v1/sources` - tenant-scoped source list and freshness
- `POST /v1/sources` - register a source using a secret reference
- `POST /v1/sources/{source_id}/validate`
- `POST /v1/sources/{source_id}/replays` - start/resume a recorded demo replay
- `GET /v1/ingestion/runs/{run_id}` - replay/live ingestion status
- `GET /v1/risk?window_start=...&category=...&bbox=...`
- `GET /v1/cells/{cell_id}/explanation?window_start=...&category=...`
- `GET /v1/model-card`

The API reads tenant-partitioned precomputed prediction Parquet files for the demo. This is faster, more stable, and easier to reproduce than performing inference on every map pan. Source-changing endpoints require an owner/admin role and create tenant-scoped audit records.

## 9. Safety and privacy gates

- Aggregate to H3 before feature generation; suppress cells/windows below a documented minimum count.
- Never return raw coordinates or event identifiers.
- Exclude protected attributes and avoid obvious proxies unless explicitly used for an audit.
- Label the output as forecast uncertainty, not ground truth or causation.
- Include a visible limitations panel and data freshness timestamp.
- Log model/data versions, not user identities.
- Prevent cross-tenant caching by including `tenant_id` in every server-side cache key and never caching authenticated API responses publicly.
- Rate-limit and quota sources per tenant; reject unbounded payloads.
- Require human review; prohibit automated enforcement and individual-level decisions.

## 10. Free integrations and MCP guidance

Do not make the core demo depend on an MCP server. MCPs are best used for development workflow, not runtime prediction.

- GitHub MCP: issue/PR coordination, repository search, and CI inspection.
- Filesystem MCP: controlled access to local papers, schemas, and artifacts if teammates use MCP clients.
- PostgreSQL MCP: optional read-only exploration only if PostGIS is introduced.
- Playwright/browser tooling: automated dashboard smoke tests and screenshots.
- Context7 or official documentation connectors: current library API lookup.

Use least-privilege tokens, pin third-party server versions, and never expose raw sensitive incident data to a remote MCP. Prefer local/open-source servers and read-only credentials. Avoid installing a connector merely to save a few lines of code; reusable Python modules and generated OpenAPI clients are easier to test and audit.

## 11. Build order

1. Freeze tenant, source, event, prediction, target-window, H3, taxonomy, and time-split contracts.
2. Build recorded replay, idempotency/checkpoints, quarantine, and two-tenant isolation tests.
3. Produce the tenant-scoped time-complete feature table and leakage tests.
4. Establish the historical-rate baseline and evaluation report.
5. Train/calibrate LightGBM and export predictions plus model card.
6. Implement tenant middleware and the typed API against fixture predictions.
7. Build the React/TypeScript/MapLibre/Motion UI against the API contract.
8. Integrate real model output, test cross-tenant denial and reduced motion, run the end-to-end smoke test, and rehearse the demo.
