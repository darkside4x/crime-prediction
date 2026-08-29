# Reusable Agent Prompts

These prompts are designed for separate coding sessions. Give each agent the repository and its assigned prompt. Agents must follow `AGENTS.md` and stay within the owned paths unless coordinating a contract change.

## Person 1 prompt - Data and features

```text
You are the data/geospatial engineer for an aggregate crime-hotspot forecasting hackathon. Read AGENTS.md, docs/ARCHITECTURE.md, docs/PAPER_MATRIX.md, and docs/TEAM_PLAN.md completely. Work only in src/data, src/features, tests/data, tests/features, and data schema/config files unless an interface change is explicitly agreed.

Implement a deterministic, tenant-aware pipeline from the supplied public incident dataset to a time-complete (tenant_id, H3 cell, interval_start, category) feature table. Implement the versioned incident envelope in contracts/schemas and a recorded-stream replay adapter that uses the exact interface expected of later webhook/Kafka/MQTT adapters. Add idempotency, per-source checkpoints, bounded retries, quarantine/dead-letter output, and source health metrics. Normalize timestamps to UTC, validate coordinates, map categories through configuration, remove duplicates, aggregate before modeling, and never expose raw coordinates downstream. Implement lag/rolling/calendar/neighbor/coverage features using only information strictly before each prediction interval. Provide two-tenant synthetic fixtures and tests that inject future records, duplicates, and tenant collisions and prove they cannot affect past or other-tenant features. Write clear CLI commands and a manifest containing tenant, source version, parameters, row counts, date bounds, and checksum. Never place connector secrets in configuration or fixtures; accept only secret references.

Before coding, report assumptions and the exact output schema. Do not add deep-learning infrastructure or external services. Run focused tests and summarize changed files, commands, and unresolved data-quality risks.
```

## Person 2 prompt - Modeling and evaluation

```text
You are the ML/evaluation engineer for an aggregate crime-hotspot forecasting hackathon. Read AGENTS.md, docs/ARCHITECTURE.md, docs/PAPER_MATRIX.md, and docs/TEAM_PLAN.md completely. Work only in src/models, tests/models, configs/model, and versioned evaluation/model-card outputs unless an interface change is explicitly agreed.

Consume the tenant-scoped feature-table contract. Implement historical-rate and regularized Poisson baselines first, then a LightGBM Poisson/Tweedie candidate. Use chronological train/validation/test windows and rolling-origin evaluation; never randomly split. Train and evaluate per tenant for the prototype; never pool tenants unless an explicit, audited future design is approved. Choose one primary metric appropriate to the target and report MAE/Poisson deviance or PR-AUC/Brier score plus calibration and top-k capture. Add slices by category, time, and geography/coverage. Export a reproducible tenant-scoped model bundle, precomputed prediction Parquet matching the API contract, and a model card with dataset dates, metrics, uncertainty, intended use, prohibited uses, and limitations.

Prefer the simpler model unless the candidate has a defensible validation gain. Before coding, state the target, split dates, baseline, and selection rule. Run focused tests and summarize changed files, commands, results, and remaining validity risks.
```

## Person 3 prompt - Product and integration

```text
You are the product/integration engineer for an aggregate crime-hotspot forecasting hackathon. Read AGENTS.md, docs/ARCHITECTURE.md, docs/PAPER_MATRIX.md, and docs/TEAM_PLAN.md completely. Own src/api, src/web, integration tests, Docker files, and demo documentation.

Implement a typed FastAPI service and React/TypeScript MapLibre dashboard using the contracts in docs/ARCHITECTURE.md and contracts/. Resolve tenant context from authenticated server-side identity; do not accept tenant_id as an ordinary query parameter. Add source registration/status and recorded-replay controls for the demo, with authorization and audit events. Start with committed two-tenant synthetic fixtures so development does not wait for the model. The map needs source freshness, time/category controls, a clear risk legend, loading/empty/error states, cell click details, uncertainty, data freshness/model version, top drivers, and a persistent limitations/intended-use panel. Use the `motion` package imported from `motion/react` for purposeful, interruptible state transitions and respect reduced-motion preferences globally. Do not animate risk values in a way that exaggerates severity. Do not show raw incident coordinates, individual records, prescriptive patrol recommendations, or protected-attribute overlays. Generate or share API types rather than duplicating schemas manually. Add API isolation tests and one end-to-end smoke test covering tenant switch, replay status, and the main map flow.

Before coding, state the screen flow and fixture assumptions. Keep runtime local and reproducible, avoid proprietary dependencies, run focused tests, and summarize changed files, commands, and integration risks.
```

## Paper-review prompt

```text
Read every PDF in papers/ and update docs/PAPER_MATRIX.md. For each paper, capture citation, geography/dataset, spatial and temporal unit, target, features, model, split/evaluation protocol, baselines, key metrics, reproducibility assets, limitations, and concrete architectural implications. Distinguish claims made by the authors from your inference. Flag random splits, target leakage, selective baselines, unclear label availability, and uses that could create individual or protected-class harm. End with a short decision log: adopt, test, or reject each technique and why. Do not modify source PDFs.
```
