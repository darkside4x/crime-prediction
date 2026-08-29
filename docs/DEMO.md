# Demo path (Person 3)

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
python -m pip install -e ".[api]"
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
cd src/web && pnpm build          # typecheck + production build
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

Copy `.env.example` to an ignored `.env` and set the server-side
`REKA_API_KEY`. The API then selects the live `reka-flash` provider; without a
key it selects the deterministic fake. Every response is schema-validated and
uncited claims are discarded, falling back to deterministic facts on any
failure. The browser never receives the API key.
