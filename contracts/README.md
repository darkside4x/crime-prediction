# Contracts

These schemas are the initial integration boundary. They use JSON Schema 2020-12 and are intentionally transport-neutral.

| Schema | Boundary |
|---|---|
| `tenant.schema.json` | tenant identity and lifecycle metadata |
| `data-source.schema.json` | tenant-owned recorded or live source definition; contains only a secret reference |
| `incident-event.schema.json` | restricted ingestion envelope emitted by every adapter |
| `feature-row.schema.json` | downstream tenant/cell/time modeling row with no raw event identity or coordinates |
| `ingestion-run.schema.json` | replay/live run progress, checkpoint, and rejection summary |
| `prediction.schema.json` | aggregate tenant-scoped prediction returned by the application API |
| `reka-source-mapping.schema.json` | human-reviewable Reka proposal for mapping a source into the incident schema |
| `reka-insight.schema.json` | grounded Reka explanation whose claims cite supplied aggregate fact IDs |

## Versioning

- `schema_version` uses semantic versioning.
- Additive optional fields are minor changes.
- Removing/renaming fields or changing meaning requires a new major version.
- Producers state a version; consumers reject unsupported major versions.
- Fixtures must validate against these files in CI.

## Security boundary

The incident event is internal and may contain raw coordinates. It must not be returned by public endpoints, logged as a payload, placed in analytics exports, or sent to remote MCP servers. Downstream records replace location with an H3 cell and omit event identifiers.

Authentication supplies `tenant_id`; schemas include it for storage, routing, audit, and validation—not as authorization proof.
