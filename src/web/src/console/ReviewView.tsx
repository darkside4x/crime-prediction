import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, newIdempotencyKey, type ReviewRequest } from "../api/client";
import { useAuth } from "./AuthContext";
import CandidateEvidence from "./CandidateEvidence";

const CATEGORIES = ["property", "violence", "public_order", "traffic_safety", "other"] as const;
const REASONS = [
  "false_positive",
  "insufficient_evidence",
  "duplicate",
  "outside_scope",
  "other",
] as const;

export default function ReviewView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;
  const queryClient = useQueryClient();

  const candidates = useQuery({
    queryKey: ["candidates", tenantId],
    queryFn: () => api.candidates(token),
  });

  const [decisionFor, setDecisionFor] = useState<string | null>(null);
  const [decision, setDecision] = useState<"confirmed" | "rejected">("confirmed");
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]>("property");
  const [reason, setReason] = useState<(typeof REASONS)[number]>("false_positive");
  const keyRef = useRef(newIdempotencyKey());

  const review = useMutation({
    mutationFn: ({ detectionId, body }: { detectionId: string; body: ReviewRequest }) =>
      api.reviewCandidate(token, detectionId, body, keyRef.current),
    onSuccess: () => {
      keyRef.current = newIdempotencyKey();
      setDecisionFor(null);
      void queryClient.invalidateQueries({ queryKey: ["candidates", tenantId] });
    },
    onError: () => {
      keyRef.current = newIdempotencyKey();
    },
  });

  const listError = candidates.error instanceof ApiError ? candidates.error : null;
  const reviewError = review.error instanceof ApiError ? review.error : null;
  const pendingCandidates =
    candidates.data?.items.filter((candidate) => candidate.review_status === "awaiting_review")
    ?? [];

  if (listError?.status === 403) {
    return (
      <section className="review-view">
        <h2 className="section-title">
          REVIEW <span className="accent">QUEUE</span>
        </h2>
        <div className="forbidden" role="alert">
          <p>
            Candidate review requires the <strong>reviewer</strong> role. Your current role is{" "}
            {session!.role}. Evidence and review decisions are restricted.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="review-view">
      <h2 className="section-title">
        REVIEW <span className="accent">QUEUE</span>
      </h2>
      <p className="muted">
        Detections below are <strong>unconfirmed candidates</strong> proposed by automated
        video analysis. Nothing here is a confirmed incident until a human reviewer decides.
        Decisions are final and immutable.
      </p>

      {candidates.isLoading && <p className="muted">Loading candidates…</p>}
      {listError && listError.status !== 403 && (
        <p role="alert" className="error-banner">
          Could not load candidates ({listError.code}).
        </p>
      )}
      {!candidates.isLoading && pendingCandidates.length === 0 && (
        <p className="muted">No candidates awaiting review.</p>
      )}

      <ul className="candidate-list">
        {pendingCandidates.map((candidate) => {
          const open = decisionFor === candidate.detection_id;
          const decided = candidate.review_status !== "awaiting_review";
          return (
            <li key={candidate.detection_id} className="candidate-card">
              <div className="row spread">
                <span className="chip chip-warn">UNCONFIRMED CANDIDATE</span>
                <span className={`chip ${decided ? "" : "chip-accent"}`}>
                  {candidate.review_status.replace(/_/g, " ")}
                </span>
              </div>
              <dl className="provenance">
                <dt>Proposed category</dt>
                <dd>{candidate.proposed_category.replace(/_/g, " ")}</dd>
                <dt>Occurred at</dt>
                <dd>{candidate.occurred_at}</dd>
                <dt>Detector</dt>
                <dd>
                  {candidate.detector_version} · confidence{" "}
                  {(candidate.confidence * 100).toFixed(0)}%
                </dd>
                <dt>Expires</dt>
                <dd>{candidate.expires_at}</dd>
                <dt>Evidence</dt>
                <dd>
                  {candidate.evidence_available
                    ? "Available to reviewers via the secured evidence flow"
                    : "Unavailable"}
                </dd>
              </dl>

              <CandidateEvidence
                token={token}
                detectionId={candidate.detection_id}
                available={candidate.evidence_available}
              />

              {decided ? (
                <p className="muted small">
                  A final review exists for this candidate; it cannot be changed.
                </p>
              ) : open ? (
                <div className="decision-form">
                  <div className="row">
                    <label>
                      <input
                        type="radio"
                        name="decision"
                        checked={decision === "confirmed"}
                        onChange={() => setDecision("confirmed")}
                      />
                      Confirm
                    </label>
                    <label>
                      <input
                        type="radio"
                        name="decision"
                        checked={decision === "rejected"}
                        onChange={() => setDecision("rejected")}
                      />
                      Reject
                    </label>
                  </div>
                  {decision === "confirmed" ? (
                    <label>
                      Confirmed category
                      <select
                        value={category}
                        onChange={(event) =>
                          setCategory(event.target.value as (typeof CATEGORIES)[number])
                        }
                      >
                        {CATEGORIES.map((item) => (
                          <option key={item} value={item}>
                            {item.replace("_", " ")}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : (
                    <label>
                      Rejection reason
                      <select
                        value={reason}
                        onChange={(event) =>
                          setReason(event.target.value as (typeof REASONS)[number])
                        }
                      >
                        {REASONS.map((item) => (
                          <option key={item} value={item}>
                            {item.replace("_", " ")}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <p className="muted small">
                    This decision is final and cannot be edited or resubmitted.
                  </p>
                  <div className="row">
                    <button
                      type="button"
                      disabled={review.isPending}
                      onClick={() =>
                        review.mutate({
                          detectionId: candidate.detection_id,
                          body:
                            decision === "confirmed"
                              ? { decision, confirmed_category: category }
                              : { decision, rejection_reason: reason },
                        })
                      }
                    >
                      {review.isPending ? "Submitting…" : "Submit final decision"}
                    </button>
                    <button type="button" className="ghost" onClick={() => setDecisionFor(null)}>
                      Cancel
                    </button>
                  </div>
                  {reviewError && (
                    <p role="alert" className="error-banner">
                      {reviewError.code === "review_final"
                        ? "A final review already exists for this candidate."
                        : `Review failed (${reviewError.code}): ${reviewError.message}`}
                    </p>
                  )}
                </div>
              ) : (
                <button type="button" onClick={() => setDecisionFor(candidate.detection_id)}>
                  Review this candidate
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
