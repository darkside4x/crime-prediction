# Data and replay pipeline

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

The SQLite state database contains raw coordinates and may contain quarantined
source payloads. It belongs under ignored `artifacts/` or `data/`, must never be
committed, and must not be served by an application endpoint. Logs and CLI output
contain counts and reason codes, not source payloads.

Only these optional event attributes survive ingestion:

- `reporting_channel`
- `source_quality`
- `coverage_status`

Every other attribute is dropped before the canonical event is persisted.

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
