import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";

interface Props {
  token: string;
  detectionId: string;
  available: boolean;
}

export default function CandidateEvidence({ token, detectionId, available }: Props) {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentUrl = useRef<string | null>(null);

  useEffect(
    () => () => {
      if (currentUrl.current) URL.revokeObjectURL(currentUrl.current);
    },
    [],
  );

  async function loadEvidence() {
    setLoading(true);
    setError(null);
    try {
      const blob = await api.candidateEvidence(token, detectionId);
      if (currentUrl.current) URL.revokeObjectURL(currentUrl.current);
      const objectUrl = URL.createObjectURL(blob);
      currentUrl.current = objectUrl;
      setUrl(objectUrl);
    } catch (caught) {
      const code = caught instanceof ApiError ? caught.code : "evidence_unavailable";
      setError(`Video evidence could not be loaded (${code}).`);
    } finally {
      setLoading(false);
    }
  }

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
