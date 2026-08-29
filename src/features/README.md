# Feature table

The feature builder converts restricted events into an aggregate, tenant-scoped
H3 cell-by-interval table. Raw coordinates, source IDs, and external event IDs do
not appear in Parquet output.

Coordinate-to-H3 conversion is performed by the data store's ingestion-boundary
method. Feature code receives only cell ID, category, occurrence time, and receipt
time; it never receives raw coordinates or source event identifiers.

## Point-in-time semantics

For a row at `interval_start = t`:

- `event_count` is the supervised label for `[t, t + interval)`.
- All lag, rolling, neighbour, and trend fields use occurrence buckets before
  `t` and only records whose `received_at` is strictly before `t`.
- `data_as_of` is the exclusive feature cutoff (`t - 1 second`), so consumers
  can enforce that every predictor is strictly prior to the target interval.
- `lag_7` means seven configured intervals, not necessarily seven days.
- `rolling_7_mean` uses the seven completed configured intervals before `t`.
- `neighbor_lag_1` is the sum across in-domain H3 ring-one neighbours in the
  immediately preceding interval.
- `recent_trend` is the mean of lags 1–3 minus the mean of lags 4–6.

The H3 domain is an explicit input. It must be derived from a fixed service area,
not from all incident locations, because using future incident locations to
construct earlier rows would leak future information.

Historical/offline builds still accept `coverage_ratio` in their build
configuration. Operational future builds do not trust that value:
`FutureFeatureBuilder` loads the latest completed per-source measured coverage,
weights it by expected seconds, and fails closed if any source has no telemetry.

## Scheduled future snapshots

`ScheduledFeatureGenerator.run(now)` aligns to the next configured UTC interval,
builds exactly one future window, validates every row against
`forecast-feature-row.schema.json`, and persists the tenant-scoped snapshot. A
future row contains no `event_count`. All event-derived predictors require both
`occurred_at` and `received_at` to be before the target, so a late-arriving or
future event cannot change an already targeted feature row.

The application scheduler should invoke this helper once per feature interval
and pass its rows to Person 2's operational inference service. Repeating a run
with the same inputs produces the same version and safely replaces the same
snapshot record.

## Provenance manifest

Each Parquet build writes a JSON manifest matching
`contracts/schemas/feature-table-manifest.schema.json`. It includes the tenant,
source versions, dataset and feature-schema versions, time bounds, row/cell
counts, checksummed input/output provenance, Git commit, dirty-tree state, and
the generation command. The modeling CLI validates this manifest and the
Parquet checksum before reading any rows.
