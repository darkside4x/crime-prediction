import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useAuth } from "./AuthContext";

export default function ModelCardView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;

  const card = useQuery({
    queryKey: ["model-card", tenantId],
    queryFn: () => api.modelCard(token),
  });

  if (card.isLoading) return <p className="muted">Loading model card…</p>;
  if (card.error || !card.data)
    return (
      <p role="alert" className="error-banner">
        Could not load the model card.
      </p>
    );

  const data = card.data;
  const beats = data.baseline_comparison.selected_model_beats_baseline;

  return (
    <section className="model-card-view">
      <h2 className="section-title">
        MODEL <span className="accent">CARD</span>
      </h2>
      <div className="panel">
        <h3>{data.model_name.replace(/_/g, " ")}</h3>
        <dl className="provenance">
          <dt>Target</dt>
          <dd>{data.target.replace(/_/g, " ")}</dd>
          <dt>Prediction unit</dt>
          <dd>{data.prediction_unit}</dd>
          <dt>Model / data version</dt>
          <dd>
            {data.model_version} · {data.data_version}
          </dd>
          <dt>Training period</dt>
          <dd>
            {data.training_period.start} → {data.training_period.end}
          </dd>
          <dt>Evaluation period (held-out, forward-in-time)</dt>
          <dd>
            {data.evaluation_period.start} → {data.evaluation_period.end}
          </dd>
          <dt>Uncertainty method</dt>
          <dd>{data.uncertainty_method}</dd>
          <dt>Suppression policy</dt>
          <dd>{data.suppression_policy}</dd>
        </dl>
      </div>

      <div className="panel">
        <h3>Baseline comparison</h3>
        <table className="compare-table">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">{data.primary_metric.name}</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{String(data.baseline_comparison.baseline_model ?? "historical rate")}</td>
              <td>{Number(data.baseline_comparison.baseline_value).toFixed(4)}</td>
            </tr>
            <tr>
              <td>{data.model_name.replace(/_/g, " ")} (selected)</td>
              <td>{Number(data.baseline_comparison.selected_value).toFixed(4)}</td>
            </tr>
          </tbody>
        </table>
        <p className={beats ? "ok-banner" : "error-banner"}>
          {beats
            ? "The selected model beats the historical-rate baseline on the held-out period."
            : "The selected model does NOT beat the historical-rate baseline; the baseline ships until it does."}
        </p>
        <p className="muted small">
          {data.primary_metric.definition} (split: {data.primary_metric.split})
        </p>
      </div>

      <div className="panel">
        <h3>Intended uses</h3>
        <ul className="limitations">
          {data.intended_uses.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <h3>Prohibited uses</h3>
        <ul className="limitations prohibited">
          {data.prohibited_uses.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <h3>Limitations</h3>
        <ul className="limitations">
          {data.limitations.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <p className="muted small">
          Feature interpretation: {data.feature_interpretation}. Human review required:{" "}
          {data.human_review_required ? "yes" : "no"}.
        </p>
      </div>
    </section>
  );
}
