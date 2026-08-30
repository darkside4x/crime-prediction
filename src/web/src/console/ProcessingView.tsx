import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { useAuth } from "./AuthContext";

function seconds(value: number): string {
  return `${Math.round(value / 60)} min`;
}

export default function ProcessingView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;
  const queryClient = useQueryClient();

  const readiness = useQuery({ queryKey: ["ready"], queryFn: api.ready, refetchInterval: 60_000 });
  const coverage = useQuery({
    queryKey: ["coverage", tenantId],
    queryFn: () => api.coverage(token),
  });
  const runs = useQuery({
    queryKey: ["ingestion-runs", tenantId],
    queryFn: () => api.ingestionRuns(token),
    refetchInterval: 1500,
  });
  const refresh = useMutation({
    mutationFn: () => api.refreshDemoForecasts(token),
    onSuccess: (result) => {
      localStorage.setItem(`demo-forecast-window:${tenantId}`, result.window_start);
      void queryClient.invalidateQueries({ queryKey: ["forecasts", tenantId] });
    },
  });
  const refreshError = refresh.error instanceof ApiError ? refresh.error : null;

  return (
    <section className="processing-view">
      <h2 className="section-title">
        Processing <span className="accent">&amp; coverage</span>
      </h2>
      <div className="panel-flow">

      <div className="panel">
        <h3>Pipeline health</h3>
        {readiness.data ? (
          <ul className="health-list">
            <li>
              <span>Overall</span>
              <span
                className={`chip ${!["ok", "ready"].includes(readiness.data.status) ? "chip-warn" : ""}`}
              >
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
            <li><span>Durable queue</span><span className="chip">{readiness.data.queue.replace(/_/g, " ")}</span></li>
            <li><span>Deployment</span><span className="chip chip-accent">{readiness.data.deployment_mode.replace(/_/g, " ")}</span></li>
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
        <div className="row spread">
          <div>
            <h3>Durable processing runs</h3>
            <p className="muted small">These rows live in Postgres and survive API or worker restarts.</p>
          </div>
          <span className="chip">polling 1.5s</span>
        </div>
        {runs.data?.items.length === 0 && <p className="muted">Upload a recording to start the worker chain.</p>}
        <ul className="run-list">
          {runs.data?.items.map((run) => (
            <li key={run.run_id}>
              <code>{run.run_id.slice(0, 8)}</code>
              <span>{run.stage}</span>
              <span className={`chip ${run.state === "failed" || run.state === "retry" ? "chip-warn" : run.state === "completed" ? "chip-ok" : "chip-accent"}`}>{run.state}</span>
            </li>
          ))}
        </ul>
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

      {session!.role === "tenant_admin" && (
        <div className="panel publish-panel">
          <div>
            <span className="eyebrow">Stage 04 · deterministic scheduler trigger</span>
            <h3>Publish the next forecast window</h3>
            <p className="muted">Builds unlabelled future features from confirmed events and measured coverage, then atomically publishes tenant forecasts.</p>
          </div>
          <button type="button" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
            {refresh.isPending ? "Publishing…" : "Publish next window"}
          </button>
          {refresh.isSuccess && (
            <p className="ok-banner">Published {refresh.data.forecast_count} forecasts for {new Date(refresh.data.window_start).toUTCString()} at {Math.round(refresh.data.coverage_ratio * 100)}% measured coverage. Open the forecast map.</p>
          )}
          {refreshError && <p className="error-banner" role="alert">{refreshError.message}</p>}
        </div>
      )}
      </div>
    </section>
  );
}
