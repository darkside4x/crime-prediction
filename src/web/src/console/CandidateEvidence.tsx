import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";

interface Props {
  token: string;
  detectionId: string;
  available: boolean;
  loadWhenReviewing?: boolean;
}

export default function CandidateEvidence({
  token,
  detectionId,
  available,
  loadWhenReviewing = false,
}: Props) {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentUrl = useRef<string | null>(null);
  const loadInFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (currentUrl.current) URL.revokeObjectURL(currentUrl.current);
    };
  }, []);

  async function loadEvidence() {
    if (loadInFlight.current) return;
    loadInFlight.current = true;
    setLoading(true);
    setError(null);
    try {
      const blob = await api.candidateEvidence(token, detectionId);
      if (!mounted.current) return;
      if (currentUrl.current) URL.revokeObjectURL(currentUrl.current);
      const objectUrl = URL.createObjectURL(blob);
      currentUrl.current = objectUrl;
      setUrl(objectUrl);
    } catch (caught) {
      if (!mounted.current) return;
      const code = caught instanceof ApiError ? caught.code : "evidence_unavailable";
      setError(`Video evidence could not be loaded (${code}).`);
    } finally {
      loadInFlight.current = false;
      if (mounted.current) setLoading(false);
    }
  }

  useEffect(() => {
    if (loadWhenReviewing && available && !currentUrl.current) {
      void loadEvidence();
    }
    // Opening the immutable decision form is the explicit evidence-load action.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadWhenReviewing, available]);

  return (
    <section className="candidate-evidence" aria-label="Candidate video evidence">
      <div className="row spread">
        <div>
          <strong>Candidate video</strong>
          <p className="muted small">
            Tenant-scoped evidence · short-lived access · review before deciding
          </p>
        </div>
        {!url && (
          <button
            type="button"
            className="ghost"
            disabled={!available || loading}
            onClick={loadEvidence}
          >
            {loading ? "Loading video…" : available ? "Load evidence video" : "Unavailable"}
          </button>
        )}
      </div>
      {url && (
        <video
          controls
          preload="metadata"
          src={url}
          aria-label={`Evidence video for candidate ${detectionId.slice(0, 8)}`}
        />
      )}
      {error && <p role="alert" className="error-banner">{error}</p>}
    </section>
  );
}
