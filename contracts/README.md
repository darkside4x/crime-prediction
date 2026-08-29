# Contract Index

JSON Schemas in `contracts/schemas/` are the authoritative integration boundary. The canonical product semantics and cross-field rules are in `docs/PHASE1_CONTRACTS.md`.

Every schema has a synthetic fixture with the same basename under `contracts/fixtures/`. Fixtures demonstrate shape only; they are not observed events, detector results, forecasts, or measured performance.

## Phase 1 product contracts

| Schema | Visibility | Boundary |
|---|---|---|
| `camera-source.schema.json` | restricted | recorded/live tenant source with secret location/connection references |
| `video-asset.schema.json` | restricted | uploaded recording or live segment metadata and retention |
| `candidate-detection.schema.json` | reviewer-only | unconfirmed Reka Vision candidate and expiring evidence reference |
| `candidate-review.schema.json` | restricted audit | immutable confirmation/rejection decision |
| `coverage-snapshot.schema.json` | aggregate/public-safe after authorization | measured source availability for one interval |
| `incident-event.schema.json` | restricted | confirmed canonical event envelope |
| `feature-row.schema.json` | internal | labelled historical training/evaluation row |
| `forecast-feature-row.schema.json` | internal | unlabelled future inference row |
| `forecast.schema.json` | public after suppression | operational future aggregate forecast |
| `tenant-context.schema.json` | internal | server-derived request tenant and role context |
| `api-error.schema.json` | public | typed safe error response |

## Existing model and AI artifact contracts

| Schema | Boundary |
|---|---|
| `tenant.schema.json` | tenant identity/lifecycle metadata |
| `data-source.schema.json` | legacy structured-event source definition |
| `ingestion-run.schema.json` | replay/live processing status |
| `feature-table-manifest.schema.json` | historical feature artifact provenance |
| `prediction.schema.json` | legacy held-out evaluation prediction; not the public operational forecast |
| `model-bundle.schema.json` | fitted estimator metadata and payload integrity |
| `model-run-manifest.schema.json` | split, selection, and artifact provenance |
| `evaluation-report.schema.json` | validation/test metrics and slices |
| `model-card.schema.json` | intended use, performance, and limitations |
| `reka-fact-bundle.schema.json` | deterministic aggregate AI facts |
| `reka-source-mapping.schema.json` | human-reviewed mapping proposal |
| `reka-insight.schema.json` | fact-cited aggregate explanation |

## Security invariants

- Authentication creates tenant context; payload tenant IDs never grant access.
- Raw footage, evidence, exact locations, coordinates, event identifiers, and secret references never cross the public forecast boundary.
- Only confirmed review decisions promote candidate detections to incident events.
- Historical feature rows have labels; future forecast feature rows never do.
- Suppressed operational forecasts use null estimates, a `suppressed` band, and no drivers.
- Reka Vision may receive tenant-approved video and produce unconfirmed candidates; it never receives exact coordinates, event IDs, credentials, secret references, or cross-tenant context.
- Reka Chat receives only approved aggregate forecast facts and never calculates forecast values.

## Versioning and changes

- Semantic versions are carried in each payload.
- Additive optional changes increment the minor version.
- Removed fields, renamed fields, or changed meaning require a new major version.
- Consumers reject unsupported major versions.
- Update fixture, schema, producers, consumers, generated types, and tests as one coordinated migration.
- Shared contract changes require review by a teammate outside the author's primary area.

## Changelog

### 2026-08-30 — allowlisted near-live HLS demo transport

- Added `hls` as an explicit live-camera transport. Public HLS sources require a
  restricted `endpoint_ref`; RTSP and ONVIF continue to require both endpoint
  and credential references.
- Browser clients cannot supply HLS URLs. The demo adapter resolves only a
  server allowlist and emits bounded `live_segment` assets through the same
  Reka Vision and candidate-review boundary as recorded uploads.
- Shared-contract review is still required before this addition is frozen.

### 2026-08-29 — Phase 1 implementation target

- Added recorded/live camera, video asset, candidate detection, immutable review, and coverage contracts.
- Separated labelled historical features from unlabelled future forecast features.
- Added an operational forecast contract distinct from held-out evaluation predictions.
- Defined server tenant context, product roles, and typed API errors.
- Defined suppression as null/unavailable rather than numeric zero risk.
- Selected Reka Vision as the managed video upload/index/search/Q&A/tagging/highlight and candidate-proposal layer, using the server-only shared `REKA_API_KEY`; numeric future forecasts remain local and deterministic.

Status remains implementation target until the required teammate review is recorded.
