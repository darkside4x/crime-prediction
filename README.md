# Crime Hotspot Forecasting - Hackathon Blueprint

This project forecasts **aggregate area-level crime risk** for a future time window. It does not predict whether a person will commit a crime, identify suspects, or recommend automated enforcement actions.

The current repository blueprint assumes:

- Input: historical incident records with timestamp, approximate location, and broad category.
- Unit of prediction: an H3 grid cell (resolution 8 by default) for the next 6- or 24-hour window.
- Output: calibrated risk or expected incident count, shown as a map with uncertainty and feature-level explanations.
- Evaluation: strictly forward-in-time, including a simple historical-rate baseline.
- Tenancy: every source, feature row, artifact, model, and API request belongs to exactly one tenant.
- Ingestion: the demo replays recorded events through the same contract used by future live adapters.
- AI experience: Reka powers schema onboarding, aggregate-data explanations, and the analyst copilot; it does not generate numeric crime-risk scores.

Start with [the system architecture](docs/ARCHITECTURE.md), [the Reka AI design](docs/REKA_AI.md), [the team plan](docs/TEAM_PLAN.md), and [the reusable agent prompts](docs/PROMPTS.md). Repository rules and interface contracts are in [AGENTS.md](AGENTS.md).

## Suggested repository layout

```text
data/                 # ignored raw data; committed schemas and samples only
contracts/            # versioned JSON Schema and API/event contracts
docs/                 # architecture, paper notes, team plan, prompts
src/
  data/               # tenant-aware ingestion, adapters, and validation
  features/           # H3/time aggregation and feature generation
  models/             # baselines, training, calibration, evaluation
  api/                # FastAPI inference and explanation endpoints
  web/                # React/TypeScript MapLibre + Motion dashboard
tests/
artifacts/             # local model/evaluation outputs; mostly ignored
```

## Definition of a successful demo

1. Select a future time window and category.
2. Display risk by grid cell on a map.
3. Click a cell to see risk, uncertainty, recent trend, and top contributing features.
4. Show a model card comparing the model with a historical-rate baseline on an untouched time period.
5. State the limitations and prohibit individual-level or automated policing use.
6. Switch between demo tenants and prove their sources, predictions, and UI state remain isolated.
7. Replay a recorded stream through the canonical ingestion contract and show freshness/health status.
8. Ask the Reka-powered copilot an aggregate question and show its cited model/data context and safety boundary.

## Papers still needed

No PDF files were present when this blueprint was created. Put them in `papers/` and add one row per paper to `docs/PAPER_MATRIX.md`. The architecture should be revised only when the evidence changes a data contract, model choice, evaluation rule, or safety requirement.
