# Web dashboard (Person 3)

React 19 + TypeScript + Vite + MapLibre GL + TanStack Query + Motion
(`motion/react`).

Design: red/near-black display style inspired by acmvit.in — big Archivo Black
type, marquee ticker, scroll-triggered (`whileInView`) and scroll-linked
(`useScroll` + `useSpring` progress bar, hero parallax) animations. All motion
respects `prefers-reduced-motion` via `<MotionConfig reducedMotion="user">`
plus `useReducedMotion` guards on parallax/marquee.

```bash
npm install
npm run dev      # http://localhost:5173, proxies /v1 -> http://localhost:8000
npm run build    # typecheck + production bundle
```

Structure:

- `src/api.ts` — typed client for the FastAPI surface (bearer-token tenants)
- `components/Hero.tsx` — parallax hero, staggered line reveal
- `components/Marquee.tsx` — looping ticker
- `components/HowItWorks.tsx` — scroll-triggered pipeline cards
- `components/Dashboard.tsx` — controls (tenant/window/category), model card, limitations
- `components/RiskMap.tsx` — MapLibre H3 choropleth with suppression styling
- `components/CellDetails.tsx` — risk, uncertainty, trend bars, drivers
- `components/Copilot.tsx` — grounded AI panel with citations and refusal states

## Reka boundary

The browser never calls Reka directly. Recorded video is uploaded to the tenant-authenticated FastAPI service, which uses the server-only `REKA_API_KEY` for Reka Vision upload/index/search/Q&A/tagging/highlights. The UI displays safe processing status and reviewed candidate records; it must not receive the key, opaque Reka video IDs, presigned Reka URLs, or secret references.

Numeric future H3 forecasts come from the local forecasting API. Reka-proposed candidates, human-confirmed incidents, and future forecasts must remain visually and textually distinct.
