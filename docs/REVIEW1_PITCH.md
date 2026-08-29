# Review 1 — Pitch Script (~3 min)

## 1. Hook (open with a fact)

"India's police forces investigate over 60 lakh cognizable crimes every year — yet patrol deployment in most cities is still decided by intuition and yesterday's FIR register. Research consistently shows crime is heavily concentrated: roughly half of urban crime occurs in about 5% of city locations. If we know *where* risk concentrates, we can put limited police resources exactly there — before incidents happen, not after."

## 2. What we are making

"We're building a **crime hotspot forecasting platform**. Given a city's historical incident records, it predicts area-level crime risk for the next 6 to 24 hours, displayed as a live risk map. To be clear about scope: we predict risk for *areas*, never for *people*. No suspect identification, no automated enforcement — this is a resource-allocation tool for analysts."

## 3. Why existing systems fail

"Existing predictive-policing tools have failed on four fronts:

1. **Black-box scores** — tools like PredPol gave numbers with no explanation or uncertainty, so analysts couldn't trust or challenge them.
2. **No honest evaluation** — many systems never proved they beat a simple 'crime happens where it happened before' baseline, and evaluated on data they'd already seen.
3. **Feedback loops and bias** — predicting where police *reported* crime sends more police there, which generates more reports, amplifying bias against over-policed areas.
4. **One-off deployments** — hardcoded to one city's data format, impossible to reuse or audit."

## 4. Our system

"Our platform addresses each failure by design.

**How it predicts:** we divide the city into hexagonal H3 grid cells and aggregate historical incidents into time-windowed features per cell — recent counts, trends, time-of-day and day-of-week patterns. Models forecast a *calibrated* risk score or expected incident count for each cell for the next window, and every forecast is evaluated strictly forward-in-time against a historical-rate baseline. If our model doesn't beat the baseline, we ship the baseline and say so.

**Features:**
- **Interactive risk map** — MapLibre dashboard; click any cell to see risk, uncertainty, recent trend, and the top contributing features. Nothing is a black box.
- **Multi-tenant by design** — one deployment serves multiple cities with fully isolated data. In our demo we run Bengaluru and Chennai side by side and prove isolation.
- **Live-ready ingestion** — recorded events replay through the same versioned contract a real feed would use, with freshness and health status.
- **AI copilot with a hard boundary** — a Reka-powered assistant answers aggregate questions and explains the data, but AI never computes a risk number. Predictions come only from evaluated models.
- **Safety guardrails in the contract** — low-support cells are suppressed rather than shown as unreliable numbers, and individual-level or automated-policing use is explicitly prohibited in the model card."

## 5. Close

"So: an explainable, honestly-evaluated, multi-city crime forecasting platform — risk maps analysts can interrogate, not black-box scores they must obey. By the demo we'll show the full loop: ingest, predict, explain, and switch tenants live. Thank you — happy to take questions."

---

*Note: verify the hook statistics before presenting — NCRB reports ~60 lakh cognizable crimes/year (IPC + SLL), and the "law of crime concentration" (Weisburd, 2015) found ~50% of crime at ~5% of street segments.*
