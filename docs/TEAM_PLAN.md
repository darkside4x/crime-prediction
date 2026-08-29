# Three-Person Product Plan

The team has one frontend owner and two backend owners. Shared contracts, architecture, and safety language require review by another teammate.

## Person 1 — Video, data, and platform backend

Owns `src/data/`, `src/features/`, video/edge workers, database migrations, broker integration, storage adapters, and their tests.

Phase 1 responsibilities:

- implement `camera-source`, `video-asset`, `candidate-detection`, `candidate-review`, and `coverage-snapshot` producers;
- implement recorded upload, Reka Vision upload/index/analyze/delete orchestration, and later RTSP/ONVIF segmentation adapters;
- implement idempotent candidate promotion into `incident-event`;
- generate historical training rows and unlabelled `forecast-feature-row` snapshots;
- enforce tenant storage partitions, retention, Postgres RLS, worker recovery, and coverage telemetry.

Acceptance: an uploaded synthetic MP4 produces reviewable candidates; one confirmation produces exactly one tenant-scoped incident; rejection produces none; replay/retry cannot duplicate it; future feature rows contain no label or restricted data.

## Person 2 — Forecasting, API, and authentication backend

Owns `src/models/`, `src/api/`, authentication/authorization, forecast orchestration, model monitoring, and their tests.

Phase 1 responsibilities:

- consume `forecast-feature-row` and produce `forecast` without accepting client tenant IDs;
- keep evaluation predictions separate from operational forecasts;
- refit selected models, calibrate probability, compute temporal uncertainty, and publish model cards;
- implement server-derived `tenant-context`, role checks, typed API errors, audit events, suppression, bounded endpoints, and server-only `REKA_API_KEY` configuration;
- generate OpenAPI types for the frontend.

Acceptance: a future snapshot produces schema-valid, versioned forecasts for windows after `data_as_of`; suppression returns null estimates; cross-tenant access and unauthorized review fail with typed errors.

## Person 3 — Frontend product

Owns `src/web/`, frontend fixtures, accessibility tests, and browser end-to-end tests.

Phase 1 responsibilities:

- consume generated API types rather than handwritten contract copies;
- implement tenant selection, source registration, video upload, live-camera setup, processing status, candidate review, coverage health, and forecast map;
- distinguish candidate detections, confirmed incidents, and forecasts everywhere;
- show uncertainty, suppression, freshness, provenance, limitations, and role-based states;
- keep motion interruptible and respect `prefers-reduced-motion`.

Acceptance: an admin can configure/upload, a reviewer can decide candidates, and a viewer can inspect forecasts without accessing evidence or source configuration.

## Phase 2 — Recorded-video vertical slice

### Phase objective

Deliver the first working product flow from an uploaded MP4 through Reka Vision to an authenticated aggregate forecast map. Production/demo video analysis uses Reka Vision. Automated tests use a deterministic fake Reka provider so they remain offline and repeatable.

```text
recorded MP4
  → restricted video asset
  → FastAPI → Reka Vision upload/index
  → Reka video Q&A/tagging/search
  → validated candidate proposals
  → human confirmation/rejection
  → confirmed incident events
  → future feature snapshot
  → operational forecast
  → H3 map
```

Live RTSP/ONVIF ingestion and large-scale infrastructure are outside Phase 2. Reka Vision recorded-video management and analysis are in scope; numeric future forecasting remains the local model's responsibility.

### Person 1 — Video, data, and platform backend

#### Implementation tasks

1. Add Postgres tables and tenant RLS for camera sources, video assets, processing runs, candidates, reviews, confirmed events, coverage snapshots, and future feature snapshots.
2. Implement recorded-video source registration using `camera-source.schema.json`.
3. Implement bounded MP4 intake with content-type, file-size, checksum, duration, consent, tenant quota, and retention validation.
4. Implement `RekaVisionProvider` to upload/index approved video, check status, run versioned candidate Q&A/tagging, and delete remote videos. It receives `REKA_API_KEY` from server configuration only.
5. Persist an opaque tenant/source/asset-to-Reka-video-ID mapping. Never expose Reka video IDs, presigned URLs, filesystem paths, or secret references through the public API.
6. Implement idempotent Reka upload, indexing, analysis, and deletion jobs with queued, running, completed, failed, cancelled, and retry states.
7. Validate Reka output into schema-valid candidate detections. Derive stable candidate IDs from tenant, asset, Reka video ID, prompt version, timestamp, and category.
8. Implement `FakeRekaVisionProvider` only for offline tests and fixture-backed development.
9. Persist candidates without identity fields, face embeddings, license plates, exact coordinates, or unrestricted frame/transcript metadata.
10. Implement immutable confirm/reject decisions and ensure one detection can have only one final review.
11. Promote confirmed reviews into exactly one canonical `IncidentEvent`; rejected or expired candidates create no event.
12. Produce measured upload/index/analysis availability snapshots. Do not silently use `coverage_ratio = 1.0`.
13. Generate point-in-time historical features and unlabelled future `forecast-feature-row` records.
14. Add retention jobs that delete Reka videos/derived assets and local transient files while preserving non-sensitive audit metadata.

#### Deliverables

- Postgres migrations and RLS policies;
- Reka Vision provider and tenant-scoped video-ID registry;
- upload/index/analyze/delete queue and workers;
- deterministic fake Reka provider for tests;
- candidate/review/promotion services;
- coverage calculation;
- future feature snapshot generator;
- worker health and processing-run metrics;
- synthetic MP4 and two-tenant fixtures.

#### Required tests

- invalid type, oversize, corrupt, checksum-mismatched, unapproved, and over-quota uploads;
- missing/invalid Reka key, access denial, timeout, indexing failure, quota/rate limit, and malformed response;
- duplicate upload/analysis job idempotency and deterministic candidate IDs across retries;
- tenant A cannot access tenant B by supplying a known Reka video ID;
- Reka output containing prompt injection, identity, guilt, or prohibited fields cannot persist;
- one immutable review per candidate;
- confirmed review creates exactly one event;
- rejected/expired review creates no event;
- tenant A cannot read or mutate tenant B video mappings, candidates, reviews, events, coverage, or features;
- `REKA_API_KEY`, Reka video IDs/URLs, raw media references, and coordinates do not enter public APIs, feature artifacts, or logs;
- future events cannot change earlier features;
- coverage duration ordering and ratio calculation;
- retention completes Reka deletion and local transient deletion without corrupting audit records; remote failure is retried and monitored.

#### Acceptance criteria

From a clean database, an approved synthetic MP4 is uploaded/indexed through Reka Vision, produces validated reviewable candidates, promotes one confirmed candidate exactly once, excludes a rejected candidate, records measured coverage, and generates a schema-valid future feature snapshot containing no `event_count` or restricted fields. Offline tests demonstrate the same flow through `FakeRekaVisionProvider`.

### Person 2 — Forecasting, API, and authentication backend

#### Implementation tasks

1. Create the FastAPI application, settings, dependency injection, health/readiness endpoints, typed errors, and request IDs.
2. Load one `REKA_API_KEY` from server-only configuration and inject it into Reka Vision and Reka Chat providers. Never serialize it or include it in OpenAPI.
3. Implement development authentication with a replaceable provider interface. Resolve `ServerTenantContext` from authenticated server-side claims.
4. Enforce `viewer`, `reviewer`, `tenant_admin`, and audited `platform_operator` permissions from the Phase 1 role matrix.
5. Implement active-tenant switching only among authorized memberships; never accept `tenant_id` as an ordinary query or body authorization field.
6. Add bounded APIs for source registration/listing, backend-proxied Reka upload/status, candidate listing, authorized evidence/highlight access, review, coverage, forecasts, and model card.
7. Require idempotency keys on mutation endpoints and emit tenant-scoped audit events for every Reka operation and review.
8. Implement the future inference service that accepts `forecast-feature-row`, validates `data_as_of < interval_start`, loads the matching tenant model bundle, and produces `forecast.schema.json`.
9. Keep held-out `prediction.schema.json` evaluation rows out of operational endpoints.
10. Implement a deterministic historical-rate operational fallback when no approved tenant model is available.
11. Apply support and coverage suppression. Suppressed forecasts must contain null estimates, a suppressed band, no drivers, and a reason.
12. Add model/data/feature/calibration versions, generation time, and freshness to every forecast.
13. For Phase 2, retain the approved existing model if available; begin final-refit, calibration, and temporal-uncertainty work without blocking the recorded vertical slice.
14. Generate and commit OpenAPI output or a repeatable generation command for Person 3.
15. Add Reka health/quota/latency/failure, audit, authorization-denial, forecast-generation, and model-fallback metrics.

#### Deliverables

- runnable FastAPI service;
- server-only shared Reka configuration and provider injection;
- authentication-provider interface and development provider;
- tenant-context and role middleware/dependencies;
- Phase 2 API routes, including backend-proxied Reka operations;
- idempotency and audit services;
- future inference service;
- historical-rate fallback;
- suppression and forecast serialization;
- OpenAPI specification/type-generation command;
- API and model integration tests.

#### Required tests

- missing, invalid, expired, and unauthorized authentication;
- missing/invalid `REKA_API_KEY`, Vision access denial, quota/rate limit, timeout, and safe degraded states;
- role matrix for every endpoint;
- tenant A cannot access tenant B resources even when supplying known IDs;
- client-supplied tenant IDs are ignored or rejected;
- duplicate mutations return the original idempotent result;
- review endpoint cannot overwrite a final decision;
- future inference rejects labelled rows and stale/non-future windows;
- model bundle tenant/version/checksum mismatch is rejected;
- fallback works without an approved model;
- suppression returns null rather than zero;
- bbox, date, category, pagination, and result limits are enforced;
- errors match `api-error.schema.json` without leaking secrets;
- the API key, Reka video ID, secret refs, and presigned Reka URLs never appear in OpenAPI or unauthorized/browser responses;
- OpenAPI contract matches committed Phase 1 schemas.

#### Acceptance criteria

An authenticated tenant can complete the backend-proxied Reka Vision workflow and request a future window. The API returns only schema-valid candidates and tenant forecasts, uses the historical fallback when necessary, suppresses unsafe outputs correctly, records Reka/model provenance and audit metadata, never exposes `REKA_API_KEY`, and denies every cross-tenant or unauthorized operation.

### Person 3 — Frontend product

#### Implementation tasks

1. Create the React/TypeScript application shell, routing, query client, error boundary, accessibility baseline, and reduced-motion policy.
2. Generate frontend API types from Person 2's OpenAPI output; do not hand-maintain duplicate domain interfaces.
3. Implement development sign-in, authorized tenant selector, active role display, and forbidden/session-expired states.
4. Implement recorded-video source onboarding and backend upload with validation, progress, cancellation, retry, Reka processing disclosure, and retention notice.
5. Implement Reka upload/index/analysis status with queued/running/completed/failed states without aggressive polling or focus theft.
6. Implement the candidate review queue. Clearly label candidates as unconfirmed and restrict evidence/decisions to authorized roles.
7. Implement confirm/reject controls with duplicate-submission protection and immutable-final-decision feedback.
8. Build the MapLibre H3 forecast map with category and future-window controls, bounded viewport requests, legend, and selection continuity.
9. Implement cell details showing expected count, occurrence probability, separate uncertainty intervals, coverage, data freshness, model/data/feature/calibration versions, drivers, and suppression reason.
10. Keep candidates, confirmed observations, and future forecasts in distinct views and visual treatments.
11. Add persistent intended-use, prohibited-use, limitations, and non-causality language.
12. Implement loading, empty, degraded-coverage, suppressed, fallback-model, stale-data, upload failure, processing failure, and API error states.
13. Add component tests, accessibility checks, and a Playwright two-tenant end-to-end test.

#### Deliverables

- authenticated application shell;
- tenant selector and role-aware navigation;
- recorded source and upload flow;
- processing-status view;
- candidate review queue;
- H3 forecast map and detail panel;
- limitations and provenance UI;
- generated API client/types;
- browser E2E and accessibility tests.

#### Required tests

- viewer/reviewer/admin controls match the role matrix;
- upload validation and progress states;
- failed processing can be retried safely;
- no browser request, source map, build artifact, or error contains `REKA_API_KEY`;
- candidate wording never presents a detection as confirmed crime;
- final review cannot be submitted twice;
- suppressed forecast is not rendered as low or zero risk;
- stale/degraded coverage is visible;
- tenant switch clears tenant-scoped query and selection state;
- raw evidence and restricted controls are absent for viewers;
- main flow works with keyboard navigation and reduced motion;
- tenant A UI cannot display cached tenant B resources.

#### Acceptance criteria

A tenant admin can register and upload a recording, a reviewer can confirm or reject generated candidates, and a viewer can inspect the next-window aggregate forecast map with uncertainty, coverage, freshness, provenance, suppression, and limitations without gaining access to restricted evidence or source configuration.

### Phase 2 integration checkpoints

| Checkpoint | Person 1 | Person 2 | Person 3 |
|---|---|---|---|
| Contract-ready | confirm storage/worker mappings | confirm API/OpenAPI mappings | confirm generated UI types/fixtures |
| Backend skeleton | migrations, Reka provider, offline fake | FastAPI, auth, tenant context, key injection | fixture-backed shell and flows |
| First vertical API | Reka upload/index/analyze → candidates → review | proxy routes, audit, future fallback | connect upload and review |
| Forecast integration | future feature snapshot | forecast generation and query | connect map and details |
| Hardening | retries, coverage, retention | isolation, limits, typed errors | accessibility, errors, tenant cache |
| Completion | worker/data tests pass | API/model tests pass | Playwright flow passes |

### Phase 2 end-to-end definition of done

The following flow passes from a clean checkout with two tenants:

1. authenticate as tenant A admin;
2. register a recorded-video source and upload the synthetic MP4;
3. wait for Reka upload, indexing, and candidate analysis to complete (offline fake in automated tests);
4. authenticate as tenant A reviewer and confirm one candidate while rejecting another;
5. verify exactly one confirmed incident is promoted;
6. generate a future feature snapshot with measured coverage;
7. generate an operational forecast using an approved model or historical fallback;
8. authenticate as tenant A viewer and inspect the forecast map and model limitations;
9. verify suppressed/stale/degraded states render correctly;
10. authenticate as tenant B and prove tenant A source, asset, candidates, evidence, reviews, events, features, forecasts, and UI cache are inaccessible.

Phase 2 is not complete based on manual screenshots alone. Focused backend tests and the browser end-to-end flow must pass, and the exact verification commands must be recorded.

## Integration sequence

1. Freeze and review `docs/PHASE1_CONTRACTS.md` plus matching schemas/fixtures.
2. Person 3 builds fixture-backed screens while backend work begins.
3. Person 1 delivers recorded MP4 to review-decision flow.
4. Person 2 delivers future snapshot to forecast API flow.
5. Integrate upload → review → confirmed event → future forecast → map.
6. Add live sources and production infrastructure only after the recorded vertical slice passes.

## Merge rules

- Fixture first for every schema change.
- No owner edits another owner's area to hide a broken contract.
- Every integration test uses at least two tenants.
- Raw video, evidence, coordinates, event identifiers, and credentials never enter public fixtures or APIs.
- No model metric or detector accuracy claim is accepted without a reproducible evaluation artifact.
