# Data, recorded-video, and replay pipeline

This directory owns the restricted ingestion boundary for recorded incident
replay. It accepts versioned `IncidentEvent` objects, verifies the source against
an authenticated tenant context, normalizes timestamps and categories, and
persists resumable state locally.

## Current data inventory

| Source | Purpose | Rights/status | Production use |
|---|---|---|---|
| `tests/data/fixtures/incidents.jsonl` | Tiny end-to-end demo | Synthetic, committed test fixture | No |
| Real incident dataset | Not selected | Must be reviewed before use | Blocked |

No result from the synthetic fixture may be presented as a real crime pattern or
model finding.

## Restricted-store boundary

Production uses `PostgresIngestionStore` and `PostgresVideoStore`. Their pooled
transactions execute `SET LOCAL app.tenant_id` before every tenant query, and
the tables in `migrations/postgres/` force row-level security. SQLite remains an
explicit development/test adapter under ignored `artifacts/` or `data/`.

Only these optional event attributes survive ingestion:

- `reporting_channel`
- `source_quality`
- `coverage_status`

Every other attribute is dropped before the canonical event is persisted.

## Recorded-video worker

`src.data.video` now implements Person 1's recorded-video boundary:

- `VideoPipelineService` registers tenant-owned recorded sources, accepts an
  approved MP4 inside a restricted media root, verifies its signature, size,
  SHA-256 checksum and server-probed duration, and enforces tenant quota and
  retention limits.
- `RekaVisionProvider` streams the approved file to Reka Vision, checks indexing
  status with `GET /v1/videos/{video_id}`, requests versioned candidate JSON with
  `POST /v1/qa/chat`, and performs remote deletion. Construct it only in backend
  composition code with `REKA_API_KEY`; the key is never part of a source or job.
- `FakeRekaVisionProvider` is deterministic and network-free for tests.
- `S3MediaStorage` uses tenant-prefixed keys, KMS encryption, checksums and opaque
  `secret://` references. `ClamAVCommandScanner` is required by production
  composition; local storage and the no-op scanner are development-only.
- `SqsJobBroker` and isolated upload/index/analyze/delete workers use persisted
  leases, heartbeats, bounded exponential backoff, explicit DLQ transfer and
  restart recovery. The database-backed broker is the local durable adapter.
- `LiveCaptureWorker` resolves RTSP/ONVIF endpoints through Secrets Manager,
  creates bounded FFmpeg segments, retries reconnects and applies queue-depth
  backpressure rather than continuously uploading a feed.
- Reka output must contain exactly offset, allowlisted aggregate category and
  confidence. Identity, plate, face, coordinate, guilt, transcript, prompt
  injection and arbitrary metadata fields fail before persistence.
- Human reviews are immutable. A confirmed review creates one canonical event;
  rejected and expired candidates create none.
- Retention deletes the Reka copy before deleting the exact local transient
  file, while retaining non-sensitive job/audit records.

Production DDL and row-level-security policies are in `migrations/postgres/`.
Run `crime-platform-migrate`, then start one process per operation, for example
`crime-video-worker --operation upload`. Set `TEST_POSTGRES_DSN` to run the
database-level cross-tenant denial test.

Coverage now comes from persisted capture/detector observations. The snapshot
formula is `detector_available_seconds / expected_seconds`; missing measurements
fail closed to zero at the forecast API boundary instead of using seeded demo
values.

The upload API owned by Person 2 should first save a bounded stream under the
configured restricted media root, then call `accept_upload`. It must not accept
a filesystem path, Reka ID, secret reference, or tenant ID from a browser as an
authorization decision.

## Replay command

From an installed development environment:

```powershell
crime-data replay `
  --source-definition tests/data/fixtures/source.json `
  --state-db artifacts/demo/ingestion.sqlite `
  --output artifacts/demo/features.parquet `
  --manifest artifacts/demo/features.manifest.json `
  --domain-cells tests/data/fixtures/domain-cells.json `
  --start 2026-08-01T00:00:00Z `
  --end 2026-08-02T00:00:00Z `
  --interval-hours 6
```

The durable checkpoint is the last consumed one-based JSONL line number.
Rerunning the command resumes after that line. Exact duplicate event IDs are
idempotent; reuse of an event ID with changed content is quarantined as an
`idempotency_conflict`.

## Failure behavior

- Unsupported or malformed records are quarantined with typed reason codes.
- Missing timezones are rejected rather than guessed from source configuration.
- `received_at` may trail `occurred_at`; a five-minute negative clock-skew
  allowance is accepted.
- SQLite operational failures use bounded exponential retry.
- A source and every event must match the authenticated tenant context.
- Reka failures record typed codes only; response bodies, media paths, remote
  IDs and the API key are excluded from errors and audit records.
