# Frontend (Person 3) — Phase 2 console

React 19 + Vite + TypeScript + MapLibre + TanStack Query + Motion. Red/near-black theme.

## Structure

- `#/` — public landing page (hero, pipeline, limitations) with a CTA into the console.
- `#/console` — authenticated product:
  - **Sign-in** — development bearer tokens; roles and tenancy resolved server-side.
  - **Forecast map** (viewer+) — H3 cells for a future window/category with uncertainty,
    suppression (grey dashed, never "low risk"), freshness/stale chip, fallback-model chip,
    detail panel with drivers and full provenance, grounded copilot.
  - **Review queue** (reviewer) — unconfirmed candidates, immutable confirm/reject with
    idempotency keys and `review_final` handling.
  - **Sources & upload** (tenant admin) — recorded-video registration, MP4 validation,
    honest `video_service_unavailable` degraded state with retry.
  - **Processing & coverage** — `/ready` health plus measured coverage snapshots.
  - **Model card** — baseline comparison rendered honestly (baseline ships if not beaten).

## Types are generated, never handwritten

- `src/api/types.gen.ts` ← `contracts/openapi.json` (openapi-typescript)
- `src/api/contracts.gen.ts` ← `contracts/schemas/*.schema.json` (json-schema-to-typescript)

Regenerate after API changes: `npm run typegen` (from `src/web`, repo root on PYTHONPATH).

## Run

```bash
# backend
PYTHONPATH=. uvicorn src.api.app:app --port 8000
# frontend
cd src/web && pnpm install && pnpm dev   # http://localhost:5173
```

Dev personas: `demo-token-one` (tenant admin, Demo One + viewer of Demo Two),
`demo-reviewer-one`, `demo-viewer-one`, `demo-token-two` (viewer, Demo Two).

## Tests

```bash
npx tsc -b          # types
npx playwright install && npm run e2e   # two-tenant browser flow (needs API on :8000)
```

Tenant switches go through `PUT /v1/me/active-tenant/{id}` and clear the entire query
cache so no tenant A state can render under tenant B. Reduced motion is honored globally
via `MotionConfig reducedMotion="user"`.
