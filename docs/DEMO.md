# Demo path (Person 3)

This file distinguishes the currently committed fixture-backed dashboard from the Phase 2 Reka Vision recorded-video flow.

The dashboard works from a clean checkout without Reka and without real model
output: predictions are deterministic fixtures that satisfy the frozen
prediction contract. Swapping in Person 2's exported Parquet only replaces
`src/api/demo_data.py` internals — no contract changes.

## One-command demo (Docker)

```bash
docker compose up --build
# dashboard: http://localhost:8080
```

## Local development

```bash
# API (terminal 1)
pip install fastapi uvicorn h3 "jsonschema[format]"
uvicorn src.api.app:app --port 8000

# Web (terminal 2)
cd src/web
npm install
npm run dev
# dashboard: http://localhost:5173  (proxies /v1 to :8000)
```

## Demo tenants

| Tenant | Bearer token | Role | Region |
|---|---|---|---|
| Demo Tenant One | `demo-token-one` | admin | Bengaluru |
| Demo Tenant Two | `demo-token-two` | analyst | Chennai |

The web UI switches tokens with the tenant chips. Grids are disjoint by
construction; `tests/api` proves tenant A cannot read tenant B's cells and that
a client-supplied `tenant_id` query parameter is rejected.

## Tests

```bash
python -m pytest tests/api        # tenant isolation, contracts, AI fail-safety
cd src/web && npm run build       # typecheck + production build
```

## 3-minute presentation script

1. Scroll the landing page: framing (aggregate risk, human review) and pipeline.
2. Pick a forecast window and category; the H3 risk surface updates.
3. Click a hot cell: risk, band, uncertainty interval, 14-day trend, drivers
   ("associations, not causes").
4. Point at a grey cell: low-support suppression, no numeric value published.
5. Switch to Demo Tenant Two: different city, disjoint grid — isolation live.
6. Model card strip: candidate vs. historical-rate baseline on an untouched
   window; today the baseline ships honestly ("beats baseline: NO").
7. Ask the copilot "How did the model do on the test window?" — cited fact IDs,
   data freshness, model version. Then ask "Which person will commit a crime?"
   — refusal with `unsafe_request`.
8. Close on the footer: forecasts, not verdicts.

## Reka

The currently committed copilot uses a deterministic fake provider so the basic map demo runs offline. Phase 2 adds real Reka Vision video management and analysis behind FastAPI.

One server-side secret is used for both capabilities:

```text
REKA_API_KEY=<secret from the Reka platform>
```

There is no separate `REKA_VISION_API_KEY` in this repository. Do not add the key to Vite, React, browser storage, committed Compose files, fixtures, or logs.

The Phase 2 video demo is:

1. tenant admin uploads an approved MP4 to FastAPI;
2. FastAPI calls Reka Vision upload with indexing enabled;
3. the UI shows upload/index/analysis status without exposing the Reka video ID;
4. Reka Q&A/tagging returns structured candidate proposals;
5. schema-invalid or prohibited output fails safely;
6. a human reviewer confirms one candidate and rejects another;
7. only the confirmed candidate becomes an incident event;
8. the local deterministic model creates the future aggregate forecast;
9. Reka Chat may explain supplied aggregate facts but never creates the numeric risk score.

Automated tests use fake Reka Vision/Chat providers and make no network calls. A separate opt-in deployment check may validate the real key, upload/index a tiny consented fixture, and delete it afterward.
