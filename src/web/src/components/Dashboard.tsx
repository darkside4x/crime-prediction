import { useState } from "react";
import { motion } from "motion/react";
import { useQuery } from "@tanstack/react-query";
import { api, DEMO_TENANTS } from "../api";
import RiskMap from "./RiskMap";
import CellDetails from "./CellDetails";
import Copilot from "./Copilot";

export default function Dashboard() {
  const [token, setToken] = useState<string>(DEMO_TENANTS[0].token);
  const [windowStart, setWindowStart] = useState<string | null>(null);
  const [category, setCategory] = useState("all");
  const [selectedCell, setSelectedCell] = useState<string | null>(null);

  const metadata = useQuery({
    queryKey: ["metadata", token],
    queryFn: () => api.metadata(token),
  });
  const sources = useQuery({
    queryKey: ["sources", token],
    queryFn: () => api.sources(token),
  });
  const modelCard = useQuery({
    queryKey: ["model-card", token],
    queryFn: () => api.modelCard(token),
  });

  const activeWindow = windowStart ?? metadata.data?.windows[0]?.window_start ?? null;

  const risk = useQuery({
    queryKey: ["risk", token, activeWindow, category],
    queryFn: () => api.risk(token, activeWindow!, category),
    enabled: Boolean(activeWindow),
  });

  const switchTenant = (nextToken: string) => {
    setToken(nextToken);
    setSelectedCell(null);
    setWindowStart(null);
  };

  const formatWindow = (iso: string) => {
    const date = new Date(iso);
    return `${date.toUTCString().slice(5, 11)} ${String(date.getUTCHours()).padStart(2, "0")}:00Z`;
  };

  return (
    <section className="dash" id="dashboard">
      <div className="container">
        <motion.p
          className="eyebrow"
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
        >
          Live demo · fixture-backed predictions
        </motion.p>
        <motion.h2
          className="section-title"
          initial={{ opacity: 0, y: 40 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.4 }}
          transition={{ duration: 0.7 }}
        >
          THE <span className="accent">RISK</span> MAP
        </motion.h2>

        <div className="dash-shell">
          <motion.div
            className="map-wrap"
            initial={{ opacity: 0, scale: 0.97 }}
            whileInView={{ opacity: 1, scale: 1 }}
            viewport={{ once: true, amount: 0.2 }}
            transition={{ duration: 0.8 }}
          >
            <RiskMap
              data={risk.data}
              selected={selectedCell}
              onSelect={setSelectedCell}
            />
            <div className="map-badge">
              {risk.data
                ? `model ${risk.data.model_version} · data as of ${risk.data.data_as_of}`
                : risk.isLoading
                  ? "loading grid…"
                  : "no data"}
            </div>
            <div className="legend">
              FORECAST RISK — NOT GROUND TRUTH
              <div className="legend-bar" />
              <div className="legend-row"><span>typical</span><span>moderate</span><span>elevated</span></div>
              <div style={{ marginTop: 6, color: "var(--cream-faint)" }}>grey = suppressed (low support)</div>
            </div>
          </motion.div>

          <div className="side">
            <div className="panel">
              <h4>Controls</h4>
              <p className="control-label">Tenant (isolated)</p>
              <div className="control-row">
                {DEMO_TENANTS.map((tenant) => (
                  <motion.button
                    key={tenant.token}
                    className={`chip${token === tenant.token ? " active" : ""}`}
                    onClick={() => switchTenant(tenant.token)}
                    whileTap={{ scale: 0.94 }}
                  >
                    {tenant.label}
                  </motion.button>
                ))}
              </div>
              <p className="control-label">Forecast window (UTC)</p>
              <div className="control-row">
                {metadata.data?.windows.map((w) => (
                  <motion.button
                    key={w.window_start}
                    className={`chip${activeWindow === w.window_start ? " active" : ""}`}
                    onClick={() => setWindowStart(w.window_start)}
                    whileTap={{ scale: 0.94 }}
                  >
                    {formatWindow(w.window_start)}
                  </motion.button>
                ))}
              </div>
              <p className="control-label">Category</p>
              <div className="control-row">
                {metadata.data?.categories.map((c) => (
                  <motion.button
                    key={c}
                    className={`chip${category === c ? " active" : ""}`}
                    onClick={() => setCategory(c)}
                    whileTap={{ scale: 0.94 }}
                  >
                    {c.replace("_", " ")}
                  </motion.button>
                ))}
              </div>
            </div>

            <div className="panel">
              <h4>Source status</h4>
              {sources.data?.sources.map((s) => (
                <div key={s.source_id}>
                  <div className="freshness">
                    <motion.span
                      className="pulse"
                      animate={{ opacity: [1, 0.3, 1] }}
                      transition={{ duration: 2, repeat: Infinity }}
                    />
                    {s.name} · {s.status}
                  </div>
                  <div className="kv" style={{ marginTop: 8 }}>
                    <span>lag</span><b>{s.freshness.lag_seconds}s</b>
                  </div>
                  <div className="kv">
                    <span>rejected</span><b>{s.freshness.rejected_count}</b>
                  </div>
                  <div className="kv">
                    <span>last event</span><b>{s.freshness.last_accepted_event_at.slice(0, 16)}Z</b>
                  </div>
                </div>
              ))}
            </div>

            <CellDetails
              token={token}
              cellId={selectedCell}
              windowStart={activeWindow ?? ""}
              category={category}
              onClose={() => setSelectedCell(null)}
            />

            <Copilot token={token} />
          </div>
        </div>

        <motion.div
          className="panel"
          style={{ marginTop: 20 }}
          initial={{ opacity: 0, y: 32 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.3 }}
          transition={{ duration: 0.6 }}
        >
          <h4>Model card — vs. naive baseline on an untouched window</h4>
          {modelCard.data && (
            <>
              <div className="metric-strip">
                <div className="metric">
                  <div className="value">{modelCard.data.primary_metric.value}</div>
                  <div className="label">{modelCard.data.primary_metric.name} · {modelCard.data.primary_metric.split}</div>
                </div>
                <div className="metric">
                  <div className="value">{modelCard.data.baseline_comparison.baseline_value}</div>
                  <div className="label">baseline ({modelCard.data.baseline_comparison.baseline_model})</div>
                </div>
                <div className="metric">
                  <div className="value" style={{ color: modelCard.data.baseline_comparison.selected_model_beats_baseline ? "#6ecb8f" : "var(--amber)" }}>
                    {modelCard.data.baseline_comparison.selected_model_beats_baseline ? "YES" : "NO"}
                  </div>
                  <div className="label">beats baseline</div>
                </div>
                <div className="metric">
                  <div className="value" style={{ fontSize: 16, paddingTop: 8 }}>{modelCard.data.model_version}</div>
                  <div className="label">model version</div>
                </div>
              </div>
              <p className="hint" style={{ marginTop: 12 }}>
                {modelCard.data.primary_metric.definition} When the candidate does not
                beat the baseline, the baseline ships — honestly.
              </p>
            </>
          )}
        </motion.div>

        <motion.div
          className="panel"
          style={{ marginTop: 20 }}
          initial={{ opacity: 0, y: 32 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.3 }}
          transition={{ duration: 0.6 }}
        >
          <h4>Limitations — read before use</h4>
          <ul className="limits">
            {metadata.data?.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}
          </ul>
        </motion.div>
      </div>
    </section>
  );
}
