import { useEffect, useMemo, useRef, useState } from "react";
import Hls from "hls.js";
import { motion } from "motion/react";
import {
  api,
  newIdempotencyKey,
  type LiveCctvSource,
  type NearLiveRun,
  type PublicCandidate,
} from "../api/client";
import { useAuth } from "../console/AuthContext";

const STAGES = [
  "Live capture",
  "Reka upload",
  "Vision indexing",
  "Candidate analysis",
  "Human review",
  "Future prediction",
] as const;

function operationRank(stage?: string) {
  if (!stage) return 0;
  if (stage.includes("upload")) return 1;
  if (stage.includes("index")) return 2;
  if (stage.includes("analy")) return 3;
  if (stage.includes("review")) return 4;
  if (stage.includes("forecast")) return 5;
  return 0;
}

export default function NearLiveReview() {
  const { session } = useAuth();
  const token = session!.token;
  const videoRef = useRef<HTMLVideoElement>(null);
  const [source, setSource] = useState<LiveCctvSource | null>(null);
  const [run, setRun] = useState<NearLiveRun | null>(null);
  const [stage, setStage] = useState(0);
  const [candidates, setCandidates] = useState<PublicCandidate[]>([]);
  const [evidenceUrl, setEvidenceUrl] = useState<string | null>(null);
  const [evidenceFor, setEvidenceFor] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [reanalyzing, setReanalyzing] = useState(false);
  const [busyCandidate, setBusyCandidate] = useState<string | null>(null);
  const [predictionWindow, setPredictionWindow] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.liveCctv(token).then(setSource).catch((caught) => {
      setError(caught instanceof Error ? caught.message : "Live source is unavailable");
    });
  }, [token, session?.activeTenantId]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !source?.playback_url) return;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source.playback_url;
      void video.play().catch(() => undefined);
      return;
    }
    if (!Hls.isSupported()) {
      setError("This browser cannot play the HLS live feed.");
      return;
    }
    const hls = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 3 });
    hls.loadSource(source.playback_url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => void video.play().catch(() => undefined));
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) setError("The public camera stream disconnected. Reload to reconnect.");
    });
    return () => hls.destroy();
  }, [source?.playback_url]);

  useEffect(() => () => {
    if (evidenceUrl) URL.revokeObjectURL(evidenceUrl);
  }, [evidenceUrl]);

  useEffect(() => {
    if (!run?.run_id || stage >= 4) return;
    const timer = window.setInterval(async () => {
      try {
        if (!run.asset_id) {
          const captureRun = await api.ingestionRun(token, run.run_id);
          setRun(captureRun);
          setStage(operationRank(captureRun.stage));
          if (captureRun.state === "failed") {
            setError(`Processing stopped: ${captureRun.error_code ?? "capture failure"}`);
          }
          return;
        }
        const listing = await api.ingestionRuns(token, 50);
        const related = listing.items.filter((item) => item.asset_id === run.asset_id);
        const latest = related.sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
        if (latest) {
          setRun(latest);
          setStage(Math.max(...related.map((item) => operationRank(item.stage)), 0));
          if (latest.state === "failed") {
            setError(`Processing stopped: ${latest.error_code ?? "worker failure"}`);
          } else {
            setError((current) =>
              current?.startsWith("Processing stopped:") ? null : current,
            );
          }
        }
        const analysisComplete = related.some(
          (item) => item.state === "completed" && item.stage.includes("analy"),
        );
        if (analysisComplete) {
          const listingCandidates = await api.candidates(token);
          setCandidates(listingCandidates.items.filter((item) => item.asset_id === run.asset_id));
          setStage(4);
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not refresh durable workers");
      }
    }, 1800);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.asset_id, stage, token]);

  const canAnalyze = source?.analysis_mode === "reka_vision";
  const pipelineRunning = run?.state === "queued" || run?.state === "running" || run?.state === "retry";
  const canReanalyze =
    canAnalyze
    && (session?.role === "tenant_admin" || session?.role === "platform_operator")
    && run?.state === "failed"
    && run.stage.includes("analy");
  const statusText = useMemo(() => {
    if (capturing) return "Capturing…";
    if (predictionWindow) return "Forecast updated";
    if (stage >= 4 && candidates.length === 0) return "Analysis complete — no incidents proposed";
    if (run) return run.stage.replaceAll("_", " ");
    return "Ready";
  }, [candidates.length, capturing, predictionWindow, run, stage]);

  async function startCapture() {
    setError(null);
    setCandidates([]);
    setPredictionWindow(null);
    setStage(0);
    setCapturing(true);
    try {
      const next = await api.startNearLiveCapture(token, 12);
      setRun(next);
      setStage(1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Live capture could not start");
    } finally {
      setCapturing(false);
    }
  }

  async function reanalyze() {
    if (!run || !canReanalyze) return;
    setReanalyzing(true);
    setError(null);
    try {
      const fresh = await api.reanalyzeRun(token, run.run_id, newIdempotencyKey());
      setRun(fresh);
      setCandidates([]);
      setStage(3);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Re-analysis could not start");
    } finally {
      setReanalyzing(false);
    }
  }

  async function playEvidence(candidate: PublicCandidate) {
    setError(null);
    try {
      const blob = await api.candidateEvidence(token, candidate.detection_id);
      if (evidenceUrl) URL.revokeObjectURL(evidenceUrl);
      setEvidenceUrl(URL.createObjectURL(blob));
      setEvidenceFor(candidate.detection_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Evidence could not be authorized");
    }
  }

  async function decide(candidate: PublicCandidate, decision: "confirmed" | "rejected") {
    setBusyCandidate(candidate.detection_id);
    setError(null);
    try {
      await api.reviewCandidate(
        token,
        candidate.detection_id,
        decision === "confirmed"
          ? {
              decision,
              confirmed_category: candidate.proposed_category === "unmapped" ? "other" : candidate.proposed_category,
              rejection_reason: null,
            }
          : { decision, confirmed_category: null, rejection_reason: "insufficient_evidence" },
        newIdempotencyKey(),
      );
      setCandidates((current) => current.map((item) =>
        item.detection_id === candidate.detection_id ? { ...item, review_status: decision } : item,
      ));
      if (decision === "confirmed") {
        const forecast = await api.refreshDemoForecasts(token);
        window.localStorage.setItem(
          `demo-forecast-window:${session!.activeTenantId}`,
          forecast.window_start,
        );
        window.localStorage.setItem(
          `demo-forecast-category:${session!.activeTenantId}`,
          candidate.proposed_category === "unmapped" ? "other" : candidate.proposed_category,
        );
        setPredictionWindow(forecast.window_start);
        setStage(5);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The final review could not be saved");
    } finally {
      setBusyCandidate(null);
    }
  }

  return (
    <section className="live-operations" id="live-operations">
      <div className="live-heading">
        <div>
          <p className="eyebrow">Live public feed · real model path</p>
          <h1>WATCH. CAPTURE. <span>VERIFY.</span> PREDICT.</h1>
          <p>Live source → Reka → human review → forecast.</p>
        </div>
        <button className="capture-button" type="button" onClick={startCapture} disabled={capturing || pipelineRunning || !canAnalyze}>
          {capturing || pipelineRunning ? "Analysis in progress…" : "Analyze next 12 seconds"}
        </button>
      </div>

      <div className="custody-rail" aria-label="Live prediction workflow">
        {STAGES.map((label, index) => (
          <div className={`custody-step${index <= stage ? " reached" : ""}`} key={label}>
            <span>{String(index + 1).padStart(2, "0")}</span><b>{label}</b>
          </div>
        ))}
      </div>

      <div className="operations-grid">
        <article className="live-monitor">
          <div className="monitor-label"><i /> LIVE · {source?.name ?? "Connecting camera…"}</div>
          <video ref={videoRef} muted controls playsInline aria-label="Live public traffic camera" />
          <div className="monitor-footer">
            <span>{source?.attribution ?? "Public traffic source"}</span>
            <span>Capture starts on analyze</span>
          </div>
        </article>

        <aside className="evidence-rail">
          <p className="eyebrow">Current operation</p>
          <h2>{statusText}</h2>
          <p>Durable worker path.</p>
          {run && <code>run {run.run_id.slice(0, 8)}</code>}
          {!canAnalyze && source && <div className="pipeline-notice warning">Real Reka Vision is not configured. Analysis is disabled; this demo never substitutes fabricated detections.</div>}
          {error && <div className="pipeline-notice error" role="alert">{error}</div>}
          {canReanalyze && (
            <button
              className="review-action evidence"
              type="button"
              disabled={reanalyzing}
              onClick={reanalyze}
            >
              {reanalyzing ? "Queueing re-analysis…" : "Re-analyze captured segment"}
            </button>
          )}
          {evidenceUrl && (
            <div className="evidence-player">
              <span>Authorized captured evidence</span>
              <video src={evidenceUrl} controls autoPlay muted playsInline />
            </div>
          )}
        </aside>
      </div>

      <div className="candidate-header">
        <div><span>Reviewer-only queue</span><h2>Unconfirmed candidate incidents</h2></div>
        <b>{candidates.length.toString().padStart(2, "0")}</b>
      </div>
      <div className="candidate-list">
        {stage >= 4 && candidates.length === 0 && <div className="candidate-empty">Analysis complete. Reka proposed no qualifying incident in this segment.</div>}
        {stage < 4 && <div className="candidate-empty">Candidates appear after analysis.</div>}
        {candidates.map((candidate) => (
          <motion.article className="candidate-row" key={candidate.detection_id} layout>
            <div className="candidate-primary">
              <span className="candidate-flag">Unconfirmed · human decision required</span>
              <h3>{candidate.proposed_category.replaceAll("_", " ")}</h3>
              <p>{new Date(candidate.occurred_at).toUTCString()}</p>
            </div>
            <div className="candidate-confidence"><span>Model confidence</span><strong>{Math.round(candidate.confidence * 100)}%</strong><small>Not probability of crime</small></div>
            <div className="candidate-actions">
              <button type="button" className="review-action evidence" onClick={() => playEvidence(candidate)}>{evidenceFor === candidate.detection_id ? "Replay evidence" : "View evidence"}</button>
              {candidate.review_status === "awaiting_review" ? <>
                <button type="button" className="review-action reject" disabled={Boolean(busyCandidate)} onClick={() => decide(candidate, "rejected")}>Reject</button>
                <button type="button" className="review-action confirm" disabled={Boolean(busyCandidate)} onClick={() => decide(candidate, "confirmed")}>Confirm &amp; predict</button>
              </> : <span className={`decision ${candidate.review_status}`}>Final: {candidate.review_status}</span>}
            </div>
          </motion.article>
        ))}
      </div>

      {predictionWindow && <div className="prediction-unlocked">
        <div><span>Future window published</span><strong>{new Date(predictionWindow).toUTCString()}</strong></div>
        <a href="#/console/map">Open updated crime-prediction map →</a>
      </div>}
      <p className="review-limitation">Aggregate risk only · human confirmation required.</p>
    </section>
  );
}
