# Person 2 API

The FastAPI service is the authenticated boundary for tenant-scoped aggregate forecasts. It derives tenant and role context from the bearer credential, rejects client-supplied tenant filters, returns `api-error.schema.json` failures, requires idempotency keys for mutations, and never serializes Reka secrets.

## Run

```bash
python -m pip install -e ".[api,model,dev]"
uvicorn src.api.app:app --env-file .env --port 8000
```

Use `demo-token-one` for the development tenant administrator, `demo-reviewer-one` for its reviewer, `demo-viewer-one` for its viewer, and `demo-token-two` for an isolated second-tenant viewer. This provider is replaceable and is not production authentication.

With no `REKA_API_KEY`, `/ready` reports `deterministic_fallback`. With a key, Reka Chat is used for aggregate, fact-cited explanations behind a 20-second default timeout and deterministic failure fallback. The key is loaded only by server settings.

## Operational forecasts

`GET /v1/forecasts` accepts a future UTC window, an allowlisted category, an optional ordered `west,south,east,north` bounding box, and bounded pagination. It consumes unlabelled `forecast-feature-row` records and returns `forecast` records. When no approved tenant bundle is promoted it uses the named historical-rate fallback. Low-support or low-coverage outputs have null estimates.

The development registry intentionally defaults to empty. Production startup rejects development authentication, in-memory rate limiting, audit/idempotency stores, fixture-backed forecasts, local video storage, and an unconfigured model registry. A deployment bootstrap must inject OIDC authentication, durable rate limiting, Postgres/RLS repositories, S3/SQS video services, measured coverage, and a checksum-verified approved-model registry. Promotion and rollback require the audited `platform_operator` role.

## OpenAPI

```bash
python -m src.api.openapi --output contracts/openapi.json
```

Generation fails if server secret names or restricted `secret_ref` fields appear in the document. Person 3 can generate frontend types from this output.

## Verify

```bash
python -m pytest tests/api tests/models
```
