# Google Stitch Prompts — Frontend Rework (from TEAM_PLAN Person 3, Phase 2)

Stitch works best with one rich prompt per screen, then short refinement follow-ups. Send the Global style block with the first screen, then reference "same theme" for the rest.

## Global style (include in first prompt)

> Style for all screens: dark professional analyst tool. Near-black background (#0A0A0C), vivid red accent (#F40C3F), coral secondary (#FF5F4A), cream text (#FFF0EB). Bold condensed display font (like Archivo Black) for headings, clean sans for body. Sharp edges, high contrast, data-dense but calm. Desktop web app. Semantic colors only for status (green=healthy, amber=degraded, grey=suppressed/unknown).

## Screen 1 — App shell + forecast map (viewer role)

> Design the main screen of a crime hotspot forecasting web app for police analysts. [Global style]
>
> Top bar: logo, tenant switcher dropdown (shows only authorized cities, e.g. Bengaluru active), current role badge ("Viewer"), user menu. Below it a control row: crime category filter, future time-window selector (Next 6h / Next 24h), and a data-freshness chip showing "data as of" timestamp with a stale-data warning state.
>
> Body: full-bleed MapLibre-style dark map of Bengaluru covered in hexagonal H3 cells shaded by forecast risk (red intensity ramp). Some hexagons are grey with a hatched pattern meaning "suppressed — insufficient support"; these must NOT look like low risk. Map legend bottom-left including the suppressed state. A persistent thin banner: "Aggregate area forecasts only. Not for individual targeting or automated enforcement."
>
> Right panel (opens on hexagon click): expected incident count, occurrence probability, two separate uncertainty intervals, recent-trend sparkline, coverage %, top contributing feature drivers as horizontal bars, and a provenance footer listing model / data / feature / calibration versions and generation time. For a suppressed cell the panel shows no numbers — only the suppression reason.

Follow-ups:
- "Show the same screen in a degraded state: coverage warning banner, fallback-model chip reading 'historical baseline in use', and stale freshness indicator."
- "Show empty and loading states for the map and detail panel."

## Screen 2 — Sign-in + tenant selection

> Same theme. Design a sign-in screen for the platform (development auth: email + password, no social login), followed by a tenant selection screen listing the cities this user belongs to as cards with role shown per tenant (Viewer / Reviewer / Tenant admin). Include forbidden (403) and session-expired states as small variants.

## Screen 3 — Video source onboarding + upload (tenant admin role)

> Same theme. Design a "Sources" screen for a tenant admin. Left: list of registered recorded-video sources with name, status, retention period. Main area: a wizard to register a recorded-video source and upload an MP4 — drag-and-drop zone, file validation messages (wrong type, too large), upload progress bar with cancel and retry, a disclosure notice that video is processed by Reka Vision, and a data-retention notice. Role badge shows "Tenant admin".

## Screen 4 — Processing status

> Same theme. Design a processing-status screen showing a pipeline for each uploaded video: Upload → Index → Analyze steps with queued / running / completed / failed states, timestamps, and a retry button on failures. Include an overall coverage/health card (upload, indexing, analysis availability as percentages). Calm design — no aggressive spinners.

## Screen 5 — Candidate review queue (reviewer role)

> Same theme. Design a review queue for AI-detected candidate incidents from video. Each card is clearly labeled "UNCONFIRMED CANDIDATE" (never worded as a confirmed crime): category, time range, source, confidence band, and an evidence preview area gated behind a "View evidence (Reviewer only)" control. Actions: Confirm and Reject buttons with a confirmation dialog warning the decision is final and immutable. Show one card in the already-decided state with locked controls. Role badge shows "Reviewer".

## Screen 6 — Model card page

> Same theme. Design a "Model card" page: model vs historical-rate baseline comparison table on a held-out forward-in-time period, calibration chart, uncertainty methodology summary, versions, and a prominent limitations section including intended-use and prohibited-use language.

## Notes for using the output

- Export to Figma for reference; do not copy Stitch's HTML — real stack is React 19 + Vite + TS + MapLibre + TanStack Query + Motion.
- Keep the three data kinds visually distinct everywhere: candidate detections (unconfirmed), confirmed incidents, and future forecasts — the team plan requires this on every screen.
- All motion must be interruptible and honor `prefers-reduced-motion`.
