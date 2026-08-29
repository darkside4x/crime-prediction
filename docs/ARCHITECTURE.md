# Product Architecture

## Product boundary

The product combines two deliberately separate capabilities:

1. footage analysis creates **unconfirmed candidate safety incidents** for human review;
2. a statistical model forecasts **future aggregate confirmed-incident volume** by tenant, H3 cell, category, and time window.

It never identifies people, scores individual criminality, infers guilt or intent, exposes victim addresses, or recommends enforcement.

The canonical integration semantics are in `docs/PHASE1_CONTRACTS.md`; JSON Schemas are authoritative.

## End-to-end system

```text
        Recorded MP4                         Tenant-owned live camera
             |                                  RTSP / ONVIF at edge
             +-------------------+----------------------+
                                 v
                    Restricted media and source layer
                    - encrypted object/segment storage
                    - endpoint and location secret refs
                                 |
                                 v
                       Video analysis workers
                    - versioned detector
                    - checkpoint and idempotency
                    - candidate only, no identity
                                 |
                                 v
                      Restricted review service
                    - expiring evidence
                    - immutable human decision
                    - confirmed candidates only
                                 |
                                 v
                    Canonical IncidentEvent boundary
                    - UTC timestamp/category/location
                    - tenant/source authorization
                    - quarantine and deduplication
                                 |
                                 v
                       H3 privacy aggregation
                    - no coordinates/event IDs downstream
                    - measured source coverage
                                 |
                 +---------------+----------------+
                 v                                v
     Historical labelled features       Future unlabelled features
          training/evaluation             scheduled inference input
                 |                                |
                 +---------------+----------------+
                                 v
                       Tenant model service
                    - chronological selection
                    - final refit and calibration
                    - temporal uncertainty
                    - suppression
                                 |
                                 v
                    Operational forecast artifacts
                                 |
                 +---------------+----------------+
                 v                                v
       Authenticated FastAPI              React/MapLibre product
       tenant context + roles             upload/review/map/health
```

## Deployment evolution

### Recorded vertical slice

- FastAPI application and background workers;
- PostgreSQL with tenant RLS;
- durable job queue;
- S3-compatible encrypted object storage or a local equivalent;
- recorded MP4 upload;
- precomputed future forecast artifacts;
- React/MapLibre UI built against generated OpenAPI types.

### Live product

- edge connector owns RTSP/ONVIF credentials and reconnect logic;
- bounded live segments enter the same restricted video-asset boundary;
- managed Postgres/object storage/broker with backups and retention enforcement;
- autoscaled workers, monitoring, drift checks, and tenant quotas.

Recorded and live sources produce the same candidate-detection contract. Candidate promotion and forecasting logic never live inside transport adapters.

## Trust boundaries

| Boundary | Permitted data | Prohibited output |
|---|---|---|
| Media/source | raw footage, secret refs, exact registered location | public API, logs, model artifacts |
| Detection/review | expiring evidence, proposed category, confidence, reviewer decision | forecast API, cross-tenant access |
| Incident ingestion | event ID, UTC time, coordinates, category | downstream raw coordinates/IDs |
| Aggregate features | H3 cell, time, category, counts, measured coverage | individual records or identity |
| Forecast API | aggregate estimates, uncertainty, coverage, versions, suppression | secret refs, raw events, candidates |

Tenant scope is resolved from `ServerTenantContext`. A payload `tenant_id` is never authorization evidence.

## Modeling boundary

The primary target is confirmed aggregate incident count for one future interval. Inputs are lagged/rolling counts, calendar cycles, neighbour history, trend, and measured coverage. All input information must exist strictly before the forecast interval.

Candidate order:

1. historical comparable-window rate;
2. regularized Poisson baseline;
3. LightGBM Poisson candidate.

Selection uses validation and rolling-origin results. The untouched test set is opened only after selection. The selected production model is then refit under a recorded policy, and probability calibration uses validation-only or out-of-fold predictions. Operational uncertainty must use a named, versioned temporal method rather than an unlabeled approximation.

Evaluation remains chronological. Required metrics include MAE, Poisson deviance, per-window top-k capture, Brier score, calibration, time/geography/category/coverage slices, and temporally appropriate confidence intervals.

## API surface

The Phase 1 endpoint list and role matrix are frozen in `docs/PHASE1_CONTRACTS.md`. Additional rules:

- source/media/review mutations require idempotency and audit records;
- collection bounds and page sizes are enforced server-side;
- evidence access is short-lived and reviewer-authorized;
- public forecasts use `forecast.schema.json`, not legacy `prediction.schema.json`;
- suppressed forecasts contain null estimates and cannot be rendered as low risk;
- raw coordinates and `secret://` references never appear in browser responses.

## Reka boundary

Reka is deferred until deterministic upload, review, forecasting, and API flows work. It may later propose source mappings or summarize supplied aggregate facts. It is never on the detector or numeric forecasting path and may not receive raw media, evidence, candidates, coordinates, identifiers, secrets, or cross-tenant context.

## Reliability and observability

- at-least-once delivery with idempotent consumers;
- ordered source checkpoints where supported;
- bounded retry and dead-letter records with safe error codes;
- worker heartbeats, queue depth, source freshness, coverage, rejection rate, inference latency, and drift metrics;
- model/data/detector/calibration/prompt versions in relevant artifacts;
- deterministic non-AI fallback for every critical path;
- media retention and deletion are testable jobs, not documentation-only policies.

## Safety gates

- human confirmation before detector output becomes incident history;
- minimum support and coverage suppression before publication;
- visible freshness, uncertainty, intended use, and limitations;
- no causal language for feature drivers;
- no automated enforcement or allocation recommendation;
- cross-tenant denial tests at storage, worker, API, cache, and artifact boundaries;
- no production accuracy claims without a lawful dataset, provenance, and reproducible chronological evaluation.
