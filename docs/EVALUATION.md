# Historical benchmark and published result

## Dataset decision

The reproducible demo benchmark uses the official City and County of San
Francisco `Police Department Incident Reports: 2018 to Present` dataset,
resource `wg3w-h783`. Its official metadata marks it as an official, published,
public dataset with the Open Data Commons Public Domain Dedication and License
(`PDDL`). It contains supervisor-approved police incident reports and documents
that locations are shifted to nearby intersections for anonymity.

This makes it lawful and useful for a San Francisco **reported-incident
benchmark**. It is not proof of underlying crime prevalence, is not
representative of another deployment geography, and must not be joined to
identity data or used for individual or enforcement decisions. A production
deployment must repeat the representativeness and legal review for its own
jurisdiction.

Official sources:

- dataset metadata: <https://data.sfgov.org/api/views/wg3w-h783>
- dataset documentation: <https://data.sfgov.org/api/views/hje8-if2w/files/21f4d2ba-7925-422b-95e1-40620d16ca17?download=true&filename=POL-0008_DataDictionary.pdf>

## Frozen benchmark scope

- API: `https://data.sfgov.org/resource/wg3w-h783.json`
- incident window: `2024-01-01T00:00:00` through `2024-07-01T00:00:00`
- bounding box: latitude `37.75..37.81`, longitude `-122.46..-122.39`
- API rows fetched: 36,433
- fixed spatial domain: 19 H3 resolution-8 cells centred on the configured
  benchmark point; the domain is fixed independently of event locations
- accepted in-domain rows: 25,226; rejected malformed/time-inconsistent rows: 1
- aggregate table: 68,780 `(cell, six-hour UTC window, category)` rows
- categories: property, violence, public order, traffic safety, other
- raw response SHA-256:
  `50381b05462c9981ad1894fb44d76a2ee82f59e5a55594b30704534c0493a921`
- aggregate feature SHA-256:
  `6ff595004b3e8856b6c125fd11062638d9368fd4352d9059bbb645bd7f67717c`

Raw row IDs and approximate coordinates remain in the temporary restricted
ingestion database. They are not committed or exported to model artifacts.
`report_datetime` gates feature visibility, so late-reported incidents cannot
alter predictors for an earlier forecast window.

The historical public dataset has no camera-uptime measurement. Its benchmark
coverage feature is therefore an explicit `1.0` source-availability assumption,
not production telemetry. Operational future snapshots fail closed and derive
coverage from completed camera/source telemetry; this benchmark cannot evaluate
the coverage component.

## Chronological result

Model selection used validation Poisson deviance. The untouched test block ran
only after selection. The selected regularized Poisson candidate improved
validation Poisson deviance by 25.11% and test Poisson deviance by 11.53% versus
the historical-rate baseline. It did **not** beat that baseline on every metric.

| Untouched test metric | Regularized Poisson | Historical rate |
|---|---:|---:|
| Poisson deviance (lower) | 1.0653 | 1.2041 |
| MAE (lower) | 0.5006 | 0.4236 |
| Per-window/category top-10% cell capture (higher) | 0.3024 | 0.3240 |
| Brier score after validation-only calibration (lower) | 0.1484 | 0.1381 |

The selected model therefore wins only on the configured primary metric. The
baseline remains better on MAE, per-window top-k capture, and Brier score. This
mixed result must remain visible in the product/model card and is not evidence
that the candidate is universally superior.

Split:

- train: 2024-01-02 00:00Z through 2024-04-19 06:00Z (41,230 rows)
- validation: 2024-04-19 12:00Z through 2024-05-25 06:00Z (13,680 rows)
- test: 2024-05-25 12:00Z through 2024-06-30 18:00Z (13,870 rows)

The machine-readable frozen summary is in
[`evaluation-results/datasf-2024h1.json`](evaluation-results/datasf-2024h1.json).

## Reproduction

Download the exact bounded API query to a restricted temporary path, then run:

```bash
curl -sS -G 'https://data.sfgov.org/resource/wg3w-h783.json' \
  --data-urlencode '$select=row_id,incident_datetime,report_datetime,incident_category,latitude,longitude' \
  --data-urlencode "\$where=incident_datetime between '2024-01-01T00:00:00' and '2024-07-01T00:00:00' AND latitude between '37.75' and '37.81' AND longitude between '-122.46' and '-122.39'" \
  --data-urlencode '$order=incident_datetime,row_id' \
  --data-urlencode '$limit=50000' \
  --output /tmp/crime-prediction-datasf-2024h1.json

python scripts/run_datasf_benchmark.py \
  --raw-json /tmp/crime-prediction-datasf-2024h1.json \
  --state-db /tmp/crime-prediction-datasf-restricted.sqlite3 \
  --work-root /tmp/crime-prediction-datasf-benchmark
```

The script validates and aggregates at the ingestion boundary, uses a
chronological holdout, fits probability calibration only from validation
predictions, freezes test metrics before final refit, and exports versioned
calibration and multi-component uncertainty sidecars.
