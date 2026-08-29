import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, type OperationalAggregateForecast } from "../api/client";
import { useAuth } from "./AuthContext";
import ForecastMap from "./ForecastMap";
import ForecastDetails from "./ForecastDetails";
import CopilotPanel from "./CopilotPanel";

const MAX_PAGES = 3; // bounded viewport requests: never fetch unbounded cell lists

function nextWindows(count: number, stepMinutes: number): string[] {
  const windows: string[] = [];
  const start = new Date();
  start.setUTCMinutes(0, 0, 0);
  for (let index = 1; index <= count; index += 1) {
    const at = new Date(start.getTime() + index * stepMinutes * 60_000);
    windows.push(at.toISOString().replace(".000Z", "Z"));
  }
  return windows;
}

function formatWindow(iso: string): string {
  const date = new Date(iso);
  return `${date.toUTCString().slice(5, 11)} ${String(date.getUTCHours()).padStart(2, "0")}:00Z`;
}

export default function ForecastView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;

  const metadata = useQuery({
    queryKey: ["metadata", tenantId],
    queryFn: () => api.metadata(token),
  });
  const readiness = useQuery({ queryKey: ["ready"], queryFn: api.ready });

  const stepMinutes = metadata.data?.forecast_window_minutes ?? 360;
  const windows = useMemo(() => nextWindows(4, stepMinutes), [stepMinutes]);
  const [windowStart, setWindowStart] = useState<string | null>(null);
  const [category, setCategory] = useState("property");
  const [selected, setSelected] = useState<string | null>(null);
  const activeWindow = windowStart ?? windows[0];

  const forecasts = useQuery({
    queryKey: ["forecasts", tenantId, activeWindow, category],
    queryFn: async () => {
      const all: OperationalAggregateForecast[] = [];
      let page = 1;
      let total = Number.POSITIVE_INFINITY;
      while (all.length < total && page <= MAX_PAGES) {
        const result = await api.forecasts(token, {
          windowStart: activeWindow,
          category,
          page,
          pageSize: 100,
        });
        total = result.total;
        all.push(...result.items);
        page += 1;
      }
      return { items: all, total, truncated: all.length < total };
    },
    enabled: Boolean(activeWindow),
  });

  const items = forecasts.data?.items;
  const selectedItem = items?.find((item) => item.forecast_id === selected) ?? null;
  const first = items?.[0];
  const dataAsOf = first?.data_as_of ?? null;
  const staleHours = dataAsOf
    ? (Date.now() - new Date(dataAsOf).getTime()) / 3_600_000
    : null;
  const usingFallback =
    readiness.data?.forecast_models === "historical_fallback_only" ||
    (first?.model_version ?? "").startsWith("historical");
  const suppressedCount = items?.filter((item) => item.suppression.suppressed).length ?? 0;

  const error = forecasts.error instanceof ApiError ? forecasts.error : null;

  return (
    <section className="forecast-view">
      <div className="forecast-controls">
        <label>
          Category
          <select value={category} onChange={(event) => { setCategory(event.target.value); setSelected(null); }}>
            {(metadata.data?.categories ?? ["property"]).map((item) => (
              <option key={item} value={item}>
                {item.replace("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <label>
          Future window
          <select
            value={activeWindow}
            onChange={(event) => { setWindowStart(event.target.value); setSelected(null); }}
          >
            {windows.map((item) => (
              <option key={item} value={item}>
                {formatWindow(item)}
              </option>
            ))}
          </select>
        </label>
        <div className="chips">
          {dataAsOf && (
            <span className={`chip ${staleHours !== null && staleHours > 24 ? "chip-warn" : ""}`}>
              data as of {formatWindow(dataAsOf)}
              {staleHours !== null && staleHours > 24 ? " · stale" : ""}
            </span>
          )}
          {usingFallback && (
            <span className="chip chip-warn" title="No approved trained model bundle is active">
              historical baseline in use
            </span>
          )}
          {suppressedCount > 0 && (
            <span className="chip">{suppressedCount} suppressed cells</span>
          )}
          {forecasts.data?.truncated && (
            <span className="chip chip-warn">
              showing {items?.length} of {forecasts.data.total} cells
            </span>
          )}
        </div>
      </div>

      <div className="dash-shell">
        <div className="map-wrap">
          {forecasts.isLoading && <p className="map-status">Loading forecasts…</p>}
          {error && (
            <div className="map-status" role="alert">
              <p>
                Could not load forecasts ({error.code}). {error.message}
              </p>
              {error.retryable && (
                <button type="button" onClick={() => void forecasts.refetch()}>
                  Retry
                </button>
              )}
            </div>
          )}
          {!forecasts.isLoading && !error && items?.length === 0 && (
            <p className="map-status">No forecast cells for this window and category.</p>
          )}
          <ForecastMap items={items} selected={selected} onSelect={setSelected} />
          <div className="legend" aria-hidden="false">
            <span className="legend-item"><i className="swatch s-low" /> low</span>
            <span className="legend-item"><i className="swatch s-typical" /> typical</span>
            <span className="legend-item"><i className="swatch s-elevated" /> elevated</span>
            <span className="legend-item"><i className="swatch s-high" /> high</span>
            <span className="legend-item"><i className="swatch s-suppressed" /> suppressed (no estimate — not zero)</span>
          </div>
        </div>
        <ForecastDetails item={selectedItem} limitations={metadata.data?.limitations ?? []} />
      </div>

      <CopilotPanel />
    </section>
  );
}
