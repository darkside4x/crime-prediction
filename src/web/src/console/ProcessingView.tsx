import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useAuth } from "./AuthContext";

function seconds(value: number): string {
  return `${Math.round(value / 60)} min`;
}

export default function ProcessingView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;

  const readiness = useQuery({ queryKey: ["ready"], queryFn: api.ready, refetchInterval: 60_000 });
  const coverage = useQuery({
    queryKey: ["coverage", tenantId],
    queryFn: () => api.coverage(token),
  });

  return (
    <section className="processing-view">
      <h2 className="section-title">
        PROCESSING <span className="accent">&amp; COVERAGE</span>
      </h2>

      <div className="panel">
        <h3>Pipeline health</h3>
        {readiness.data ? (
          <ul className="health-list">
            <li>
              <span>Overall</span>
              <span className={`chip ${readiness.data.status !== "ok" ? "chip-warn" : ""}`}>
                {readiness.data.status}
              </span>
            </li>
            <li>
              <span>Video intake</span>
              <span
                className={`chip ${readiness.data.video_service !== "connected" ? "chip-warn" : ""}`}
              >
                {readiness.data.video_service.replace(/_/g, " ")}
              </span>
            </li>
            <li>
              <span>Forecast models</span>
              <span
                className={`chip ${
                  readiness.data.forecast_models !== "trained_models_active" ? "chip-warn" : ""
                }`}
              >
                {readiness.data.forecast_models.replace(/_/g, " ")}
              </span>
            </li>
            <li>
              <span>Reka chat</span>
              <span className="chip">{readiness.data.reka_chat.replace(/_/g, " ")}</span>
            </li>
          </ul>
        ) : (
          <p className="muted">Reading service health…</p>
        )}
        <p className="muted small">
          Health states are reported honestly: a degraded stage is shown as degraded rather
          than assumed to be fine.
        </p>
      </div>

      <div className="panel">
        <h3>Source coverage</h3>
        {coverage.isLoading && <p className="muted">Loading coverage snapshots…</p>}
        {coverage.error && (
          <p role="alert" className="error-banner">
            Could not load coverage.
          </p>
        )}
        {coverage.data?.items.length === 0 && (
          <p className="muted">No coverage snapshots yet for this tenant.</p>
        )}
        {coverage.data?.items.map((snapshot) => {
          const pct = Math.round(snapshot.coverage_ratio * 100);
          const degraded = snapshot.degraded_reason_codes.length > 0 || pct < 80;
          return (
            <div key={`${snapshot.source_id}-${snapshot.interval_start}`} className="coverage-card">
              <div className="row spread">
                <strong>Source {snapshot.source_id.slice(0, 8)}</strong>
                <span className={`chip ${degraded ? "chip-warn" : ""}`}>{pct}% coverage</span>
              </div>
              <div
                className="coverage-bar"
                role="img"
                aria-label={`Coverage ${pct} percent — measured, not assumed`}
              >
                <i style={{ width: `${pct}%` }} />
              </div>
              <dl className="provenance">
                <dt>Interval</dt>
                <dd>
                  {snapshot.interval_start} → {snapshot.interval_end}
                </dd>
                <dt>Expected / connected</dt>
                <dd>
                  {seconds(snapshot.expected_seconds)} / {seconds(snapshot.connected_seconds)}
                </dd>
                <dt>Processable / detector-available</dt>
                <dd>
                  {seconds(snapshot.processable_seconds)} /{" "}
                  {seconds(snapshot.detector_available_seconds)}
                </dd>
                {snapshot.degraded_reason_codes.length > 0 && (
                  <>
                    <dt>Degraded reasons</dt>
                    <dd>{snapshot.degraded_reason_codes.join(", ")}</dd>
                  </>
                )}
                <dt>Computed at</dt>
                <dd>{snapshot.computed_at}</dd>
              </dl>
            </div>
          );
        })}
        <p className="muted small">
          Coverage is measured observation time, never assumed to be 100%. Forecasts built on
          low-coverage intervals are suppressed or flagged.
        </p>
      </div>
    </section>
  );
}
