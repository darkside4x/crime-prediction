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

The near-live workflow requires `ffmpeg` in the API container/host and a
server-side `REKA_API_KEY`. Without the key, the application still captures the
real public segment but clearly labels candidate analysis as deterministic test
output.

## Local development

```bash
# API (terminal 1)
python -m pip install -e ".[api,model]"
uvicorn src.api.app:app --env-file .env --port 8000

# Web (terminal 2)
cd src/web
pnpm install
pnpm dev
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
cd src/web && pnpm build           # typecheck + production build
```

## 3-minute presentation script

1. Open **CCTV review** and click **Capture 20 seconds**. Point out the custody
   rail: HLS capture → bounded MP4 → Reka upload/index → schema validation →
   human review. The source URL is server-allowlisted and never supplied by the browser.
2. Confirm or reject an unconfirmed candidate. Reka confidence is explicitly
   not presented as probability that a crime occurred.
3. Scroll the landing page: framing (aggregate risk, human review) and pipeline.
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
10. Close on the footer: forecasts, not verdicts.

## Near-live API check

Start the services, configure `REKA_API_KEY` in `.env`, then run:

```bash
curl -sS -X POST http://localhost:8000/v1/demo/near-live-cctv/captures \
  -H 'Authorization: Bearer demo-token-one' \
  -H 'Idempotency-Key: near-live-demo-0001' \
  -H 'Content-Type: application/json' \
  -d '{"source_key":"louisiana-dot-i20","duration_seconds":20}'
```

Poll the returned run ID:

```bash
curl -sS http://localhost:8000/v1/ingestion/runs/REPLACE_RUN_ID \
  -H 'Authorization: Bearer demo-token-one'
```

The fixed demo feed is a public LADOTD/511 Louisiana HLS camera. Availability
is outside this application's control. Public reachability does not grant a
general redistribution licence: keep the segment restricted, attribute the
source, apply the one-day demo retention policy, and do not publish raw footage.

## Reka

The copilot selects live Reka Chat when a server-side key is configured and otherwise uses a deterministic provider so the basic map demo runs offline. Phase 2 adds real Reka Vision video management and analysis behind FastAPI.

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

Automated tests use fake HLS capture and fake Reka Vision/Chat providers and
make no network calls. The real demo uses the same services with the allowlisted
HLS adapter and server-only Reka key.

The feed provenance is LADOTD's official `GET /api/v2/get/cameras` catalogue:
source `101`, view `2206`, whose documented `VideoUrl` is the server-side HLS
playlist used by the adapter. The catalogue endpoint itself requires a 511LA
developer key and is not called at runtime; the bounded segment is fetched
directly from the official public HLS `VideoUrl`. Do not describe this as an
unkeyed catalogue API, and obtain permission before uploading public footage to
Reka or retaining it beyond the live demonstration.
