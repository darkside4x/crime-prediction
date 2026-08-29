import type { OperationalAggregateForecast } from "../api/client";

interface Props {
  item: OperationalAggregateForecast | null;
  limitations: string[];
}

function interval(estimate: { lower: number | null; upper: number | null; interval_level: number }): string {
  if (estimate.lower === null || estimate.upper === null) return "—";
  return `${estimate.lower.toFixed(2)} – ${estimate.upper.toFixed(2)} (${Math.round(estimate.interval_level * 100)}%)`;
}

export default function ForecastDetails({ item, limitations }: Props) {
  if (!item) {
    return (
      <aside className="cell-details" aria-live="polite">
        <p className="muted">Select a cell to inspect its forecast, uncertainty, and provenance.</p>
      </aside>
    );
  }

  if (item.suppression.suppressed) {
    return (
      <aside className="cell-details" aria-live="polite">
        <h3>Cell {item.cell_id.slice(-6)}</h3>
        <p className="suppressed-note" role="status">
          <strong>Estimate suppressed</strong> — reason:{" "}
          {item.suppression.reason ?? "policy"}. No numeric value is available for this
          cell. Suppression must not be read as low or zero risk.
        </p>
        <dl className="provenance">
          <dt>Coverage ratio</dt>
          <dd>{(item.coverage_ratio * 100).toFixed(0)}%</dd>
          <dt>Model</dt>
          <dd>{item.model_version}</dd>
          <dt>Data as of</dt>
          <dd>{item.data_as_of}</dd>
        </dl>
      </aside>
    );
  }

  return (
    <aside className="cell-details" aria-live="polite">
      <h3>
        Cell {item.cell_id.slice(-6)} · <span className={`band band-${item.risk_band}`}>{item.risk_band}</span>
      </h3>
      <dl className="metrics">
        <dt>Expected incidents</dt>
        <dd>
          {item.expected_count.value?.toFixed(2) ?? "—"}
          <span className="muted"> · interval {interval(item.expected_count)}</span>
        </dd>
        <dt>Occurrence probability</dt>
        <dd>
          {item.occurrence_probability.value !== null
            ? `${(item.occurrence_probability.value * 100).toFixed(0)}%`
            : "—"}
          <span className="muted"> · interval {interval(item.occurrence_probability)}</span>
        </dd>
        <dt>Coverage</dt>
        <dd>{(item.coverage_ratio * 100).toFixed(0)}% of expected observation time</dd>
      </dl>
      {item.drivers.length > 0 && (
        <>
          <h4>Associated features</h4>
          <ul className="drivers">
            {item.drivers.map((driver) => (
              <li key={driver.feature}>
                <span>{driver.feature.replace(/_/g, " ")}</span>
                <span className={driver.direction === "higher" ? "up" : "down"}>
                  {driver.direction === "higher" ? "↑ pushes risk up" : "↓ pushes risk down"}
                </span>
              </li>
            ))}
          </ul>
          <p className="muted small">Associations, not causes. Human interpretation required.</p>
        </>
      )}
      <details className="provenance-block">
        <summary>Provenance</summary>
        <dl className="provenance">
          <dt>Window</dt>
          <dd>
            {item.window_start} → {item.window_end}
          </dd>
          <dt>Generated at</dt>
          <dd>{item.generated_at}</dd>
          <dt>Data as of</dt>
          <dd>{item.data_as_of}</dd>
          <dt>Model version</dt>
          <dd>{item.model_version}</dd>
          <dt>Data version</dt>
          <dd>{item.data_version}</dd>
          <dt>Feature snapshot</dt>
          <dd>{item.feature_snapshot_version}</dd>
          <dt>Estimate method</dt>
          <dd>{item.expected_count.method}</dd>
          <dt>Calibration</dt>
          <dd>{item.occurrence_probability.calibration_version ?? "uncalibrated"}</dd>
        </dl>
      </details>
      {limitations.length > 0 && (
        <details>
          <summary>Limitations</summary>
          <ul className="limitations">
            {limitations.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      )}
    </aside>
  );
}
