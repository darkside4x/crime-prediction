import { AnimatePresence, motion } from "motion/react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

interface Props {
  token: string;
  cellId: string | null;
  windowStart: string;
  category: string;
  onClose: () => void;
}

export default function CellDetails({ token, cellId, windowStart, category, onClose }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ["explanation", token, cellId, windowStart, category],
    queryFn: () => api.explanation(token, cellId!, windowStart, category),
    enabled: Boolean(cellId),
  });

  const maxCount = Math.max(1, ...(data?.recent_trend.map((t) => t.count) ?? [1]));

  return (
    <div className="panel">
      <h4>Cell details</h4>
      <AnimatePresence mode="wait">
        {!cellId ? (
          <motion.p key="empty" className="hint" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            Click a hexagon on the map to see its risk, uncertainty, recent trend, and
            top contributing features.
          </motion.p>
        ) : isLoading || !data ? (
          <motion.p key="loading" className="hint" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            Loading…
          </motion.p>
        ) : (
          <motion.div
            key={cellId}
            initial={{ opacity: 0, x: 24 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -12 }}
            transition={{ duration: 0.3 }}
          >
            {data.prediction.suppressed ? (
              <p className="hint">
                This cell/window is below the aggregate support threshold and is
                suppressed — no numeric value is published.
              </p>
            ) : (
              <>
                <div style={{ display: "flex", alignItems: "baseline" }}>
                  <span className="risk-number">
                    {(data.prediction.risk! * 100).toFixed(0)}%
                  </span>
                  <span className="band-tag">{data.prediction.risk_band}</span>
                </div>
                <div style={{ marginTop: 14 }}>
                  <div className="kv"><span>Expected count</span><b>{data.prediction.expected_count}</b></div>
                  <div className="kv">
                    <span>Uncertainty</span>
                    <b>{data.prediction.uncertainty!.lower} – {data.prediction.uncertainty!.upper}</b>
                  </div>
                  <div className="kv"><span>Cell</span><b style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{data.prediction.cell_id}</b></div>
                </div>
                <p className="control-label" style={{ marginTop: 16 }}>Recent 14-day trend (counts)</p>
                <div className="trend" aria-hidden>
                  {data.recent_trend.map((t, i) => (
                    <motion.div
                      key={t.date}
                      className="trend-bar"
                      initial={{ scaleY: 0 }}
                      animate={{ scaleY: Math.max(0.06, t.count / maxCount) }}
                      transition={{ delay: i * 0.02, duration: 0.35 }}
                      style={{ transformOrigin: "bottom", height: 56 }}
                    />
                  ))}
                </div>
                <p className="control-label" style={{ marginTop: 16 }}>Top drivers (associations, not causes)</p>
                {data.prediction.drivers!.map((d) => (
                  <div className="driver" key={d.feature}>
                    <span>{d.feature}</span>
                    <span className={`dir-${d.direction}`}>{d.direction === "higher" ? "▲ higher" : "▼ lower"}</span>
                  </div>
                ))}
              </>
            )}
            <button className="chip" style={{ marginTop: 14 }} onClick={onClose}>
              Clear selection
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
