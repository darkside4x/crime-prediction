# Contracts

These schemas are the initial integration boundary. They use JSON Schema 2020-12 and are intentionally transport-neutral.

| Schema | Boundary |
|---|---|
| `tenant.schema.json` | tenant identity and lifecycle metadata |
| `data-source.schema.json` | tenant-owned recorded or live source definition; contains only a secret reference |
| `incident-event.schema.json` | restricted ingestion envelope emitted by every adapter |
| `feature-row.schema.json` | downstream tenant/cell/time modeling row with no raw event identity or coordinates |
| `feature-table-manifest.schema.json` | tenant-scoped feature artifact provenance consumed by training |
| `ingestion-run.schema.json` | replay/live run progress, checkpoint, and rejection summary |
| `prediction.schema.json` | aggregate tenant-scoped prediction returned by the application API |
| `model-bundle.schema.json` | fitted-estimator metadata, feature order, serializer, and payload integrity |
| `model-run-manifest.schema.json` | chronological split, candidate selection, and checksummed model artifacts |
| `evaluation-report.schema.json` | validation/test metrics, calibration, slices, and spatial error audit |
| `model-card.schema.json` | structured intended use, prohibited use, performance, and limitations |
| `reka-fact-bundle.schema.json` | deterministic aggregate facts supplied to Reka; suppressed values are null |
| `reka-source-mapping.schema.json` | human-reviewable Reka proposal for mapping a source into the incident schema |
| `reka-insight.schema.json` | grounded Reka explanation whose claims cite supplied aggregate fact IDs |

Representative payloads live in `contracts/fixtures/`. Fixtures are synthetic interface examples, not measured model results. Each fixture uses the schema basename with `.schema` omitted.

## Modeling artifact flow

```text
feature-row + feature-table-manifest
                |
                v
       model-run-manifest
          /     |      \
         v      v       v
 evaluation  model-card  prediction
         \      |       /
          v     v      v
          reka-fact-bundle
                  |
                  v
             reka-insight
```

- Feature tables and all downstream artifacts contain exactly one `tenant_id`.
- `data_version` identifies the feature dataset used for training or inference; `model_version` identifies the selected trained artifact.
- Model selection uses validation/rolling-origin results. The untouched test window is evaluated only after selection.
- Reka receives only facts already computed by deterministic code. It does not derive missing values or alter predictions.
- A suppressed fact must set `suppressed: true` and `value: null`.

## Versioning

- `schema_version` uses semantic versioning.
- Additive optional fields are minor changes.
- Removing/renaming fields or changing meaning requires a new major version.
- Producers state a version; consumers reject unsupported major versions.
- Fixtures must validate against these files in CI.

The current major versions are:

- `feature-row`, `prediction`, `reka-insight`, and `reka-source-mapping`: `2.0.0`.
- All other schemas: `1.0.0`.

## Security boundary

The incident event is internal and may contain raw coordinates. It must not be returned by public endpoints, logged as a payload, placed in analytics exports, or sent to remote MCP servers. Downstream records replace location with an H3 cell and omit event identifiers.

Authentication supplies `tenant_id`; schemas include it for storage, routing, audit, and validation—not as authorization proof.
