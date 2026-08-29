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

`coverage_ratio` is currently supplied by the build configuration. A real source
must compute it from documented source-availability telemetry before the feature
is used for modeling.

## Provenance manifest

Each Parquet build writes a JSON manifest matching
`contracts/schemas/feature-table-manifest.schema.json`. It includes the tenant,
source versions, dataset and feature-schema versions, time bounds, row/cell
counts, checksummed input/output provenance, Git commit, dirty-tree state, and
the generation command. The modeling CLI validates this manifest and the
Parquet checksum before reading any rows.
