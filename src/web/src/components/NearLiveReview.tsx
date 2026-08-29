import { useEffect, useMemo, useState } from "react";
import { motion } from "motion/react";
import { api, newIdempotencyKey, type NearLiveRun, type PublicCandidate } from "../api/client";
import { useAuth } from "../console/AuthContext";

type CandidateDetection = PublicCandidate;

const STAGES = [
  ["capturing_hls", "Capture"],
  ["segment_validated", "Segment"],
  ["reka_upload", "Reka upload"],
  ["reka_indexing", "Index + validate"],
  ["awaiting_human_review", "Human review"],
] as const;

function stageIndex(stage?: string) {
  if (!stage || stage === "capture_queued") return 0;
  const found = STAGES.findIndex(([key]) => key === stage);
  return found < 0 ? 0 : found;
}

export default function NearLiveReview() {
  const { session } = useAuth();
  const token = session!.token;
  const [run, setRun] = useState<NearLiveRun | null>(null);
  const [candidates, setCandidates] = useState<CandidateDetection[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busyCandidate, setBusyCandidate] = useState<string | null>(null);

  const activeStage = useMemo(() => stageIndex(run?.stage), [run?.stage]);

  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.ingestionRun(token, run.run_id);
        setRun(next);
        if (next.state === "completed") {
          const listing = await api.candidates(token);
          setCandidates(
            listing.items.filter((candidate) => candidate.asset_id === next.asset_id),
          );
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not refresh the processing run");
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.state, token]);

  async function startCapture() {
    setError(null);
    setCandidates([]);
    try {
      setRun(await api.startNearLiveCapture(token, 20));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start the public camera capture");
    }
  }

  async function decide(candidate: CandidateDetection, decision: "confirmed" | "rejected") {
    setBusyCandidate(candidate.detection_id);
    setError(null);
    try {
      await api.reviewCandidate(
        token,
        candidate.detection_id,
        decision === "confirmed"
          ? {
              decision,
              confirmed_category:
                candidate.proposed_category === "unmapped"
                  ? "other"
                  : candidate.proposed_category,
              rejection_reason: null,
            }
          : {
              decision,
              confirmed_category: null,
              rejection_reason: "insufficient_evidence",
            },
        newIdempotencyKey(),
      );
      setCandidates((current) =>
        current.map((item) =>
          item.detection_id === candidate.detection_id
            ? { ...item, review_status: decision }
            : item,
        ),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Review could not be saved");
    } finally {
      setBusyCandidate(null);
    }
  }

  return (
    <section className="review-workbench" id="near-live">
      <div className="container">
        <p className="eyebrow">Real public feed · restricted review workflow</p>
        <div className="review-heading">
          <div>
            <h2>NEAR-LIVE <span>CCTV</span> SEGMENT</h2>
            <p>
              Captures one bounded segment from an allowlisted Louisiana DOT HLS feed.
              This is not continuous live monitoring.
            </p>
          </div>
          <motion.button
            type="button"
            className="capture-button"
            onClick={startCapture}
            disabled={run?.state === "queued" || run?.state === "running"}
            whileTap={{ scale: 0.97 }}
          >
            {run?.state === "queued" || run?.state === "running"
              ? "Pipeline running…"
              : "Capture 20 seconds"}
          </motion.button>
        </div>

        <div className="custody-rail" aria-label="Video processing stages">
          {STAGES.map(([, label], index) => (
            <div
              className={`custody-step${index <= activeStage && run ? " reached" : ""}`}
              key={label}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <b>{label}</b>
            </div>
          ))}
        </div>

        <div className="review-status" aria-live="polite">
          <div>
            <span className={`status-lamp ${run?.state ?? "idle"}`} />
            <strong>{run ? run.stage.replaceAll("_", " ") : "Ready to capture"}</strong>
          </div>
          <div className="status-meta">
            {run?.source_attribution ?? "LADOTD / 511 Louisiana"}
            {run && ` · ${run.analysis_mode === "reka_vision" ? "Reka Vision" : "offline test analyzer"}`}
          </div>
        </div>

        {run?.analysis_mode === "deterministic_fake" && (
          <div className="pipeline-notice warning">
            Reka Vision is not configured on the server. The real footage is captured, but candidate
            analysis is deterministic test output until the server key is added.
          </div>
        )}
        {error && <div className="pipeline-notice error">{error}</div>}

        <div className="candidate-header">
          <div>
            <span>Restricted queue</span>
            <h3>Unconfirmed candidates</h3>
          </div>
          <b>{candidates.length.toString().padStart(2, "0")}</b>
        </div>

        <div className="candidate-list">
          {run?.state === "completed" && candidates.length === 0 && (
            <div className="candidate-empty">
              No candidate safety incident was proposed for this segment. This is a valid result.
            </div>
          )}
          {!run && (
            <div className="candidate-empty">
              Start a capture to create a bounded MP4, send it through Reka, and populate this queue.
            </div>
          )}
          {candidates.map((candidate) => (
            <article className="candidate-row" key={candidate.detection_id}>
              <div className="candidate-primary">
                <span className="candidate-flag">Unconfirmed · human decision required</span>
                <h4>{candidate.proposed_category.replaceAll("_", " ")}</h4>
                <p>{new Date(candidate.occurred_at).toUTCString()}</p>
              </div>
              <div className="candidate-confidence">
                <span>Analysis confidence</span>
                <strong>{Math.round(candidate.confidence * 100)}%</strong>
                <small>Not probability of crime</small>
              </div>
              <div className="candidate-actions">
                {candidate.review_status === "awaiting_review" ? (
                  <>
                    <button
                      type="button"
                      className="review-action reject"
                      disabled={busyCandidate === candidate.detection_id}
                      onClick={() => decide(candidate, "rejected")}
                    >
                      Reject
                    </button>
                    <button
                      type="button"
                      className="review-action confirm"
                      disabled={busyCandidate === candidate.detection_id}
                      onClick={() => decide(candidate, "confirmed")}
                    >
                      Confirm
                    </button>
                  </>
                ) : (
                  <span className={`decision ${candidate.review_status}`}>
                    Final: {candidate.review_status}
                  </span>
                )}
              </div>
            </article>
          ))}
        </div>

        <p className="review-limitation">
          “Near-live CCTV segment” means a short clip captured from a live public traffic feed.
          Reka proposes candidates only; it does not confirm crime or calculate future area risk.
        </p>
      </div>
    </section>
  );
}
