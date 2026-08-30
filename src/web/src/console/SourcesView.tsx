import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, newIdempotencyKey, type SourceMapLocation } from "../api/client";
import { useAuth } from "./AuthContext";
import NearLiveReview from "../components/NearLiveReview";
import SourceLocationMap from "./SourceLocationMap";

const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;

export default function SourcesView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;
  const queryClient = useQueryClient();
  const [mappedSource, setMappedSource] = useState<SourceMapLocation | null>(null);
  const location = useMutation({
    mutationFn: (sourceId: string) => api.sourceMapLocation(token, sourceId),
    onSuccess: setMappedSource,
  });

  const sources = useQuery({
    queryKey: ["sources", tenantId],
    queryFn: () => api.sources(token),
  });

  const [name, setName] = useState("");
  const [retention, setRetention] = useState(7);
  // The registered-location catalogue is a Phase 3 backend feature; the demo
  // fixture location is the only allow-listed value the API accepts today.
  const locationId = "30000000-0000-4000-8000-000000000001";
  const registerKey = useRef(newIdempotencyKey());

  const register = useMutation({
    mutationFn: () =>
      api.createRecordedSource(
        token,
        {
          name,
          timezone: "Asia/Kolkata",
          registered_location_id: locationId,
          retention_policy_days: retention,
        },
        registerKey.current,
      ),
    onSuccess: () => {
      registerKey.current = newIdempotencyKey();
      setName("");
      void queryClient.invalidateQueries({ queryKey: ["sources", tenantId] });
    },
  });

  const [file, setFile] = useState<File | null>(null);
  const [uploadSourceId, setUploadSourceId] = useState("");
  const [consentConfirmed, setConsentConfirmed] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const uploadKey = useRef(newIdempotencyKey());
  const upload = useMutation({
    mutationFn: () => {
      if (!file || !uploadSourceId || !consentConfirmed) {
        throw new Error("Upload prerequisites are incomplete");
      }
      const now = Date.now();
      const capturedStart = new Date(
        Math.min(file.lastModified || now - 60_000, now - 1_000),
      ).toISOString();
      return api.uploadVideo(
        token,
        {
          sourceId: uploadSourceId,
          capturedStart,
          capturedEnd: new Date(now).toISOString(),
          consentConfirmed,
          file,
        },
        uploadKey.current,
      );
    },
    onSettled: () => {
      uploadKey.current = newIdempotencyKey();
    },
  });

  const onFileChange = (selected: File | null) => {
    setFileError(null);
    if (!selected) {
      setFile(null);
      return;
    }
    if (!selected.type.startsWith("video/mp4") && !selected.name.endsWith(".mp4")) {
      setFileError("Only MP4 recordings are accepted.");
      setFile(null);
      return;
    }
    if (selected.size > MAX_UPLOAD_BYTES) {
      setFileError("File exceeds the 512 MB upload bound.");
      setFile(null);
      return;
    }
    setFile(selected);
  };

  const submitRegister = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim().length > 0) register.mutate();
  };

  const registerError = register.error instanceof ApiError ? register.error : null;
  const uploadError = upload.error instanceof ApiError ? upload.error : null;

  return (
    <section className="sources-view">
      <h2 className="section-title">
        SOURCES <span className="accent">&amp; UPLOAD</span>
      </h2>

      <div className="sources-grid">
        <div className="panel">
          <h3>Registered sources</h3>
          {sources.isLoading && <p className="muted">Loading sources…</p>}
          {sources.error && (
            <p role="alert" className="error-banner">
              Could not load sources.
            </p>
          )}
          {sources.data?.items.length === 0 && (
            <p className="muted">No sources registered for this tenant yet.</p>
          )}
          <ul className="source-list">
            {sources.data?.items.map((source) => (
              <li key={source.source_id}>
                <strong>{source.name}</strong>
                <span className="muted">
                  {source.mode} · {source.status} · retention {source.retention_policy_days}d
                </span>
                <button
                  type="button"
                  className="ghost source-location-button"
                  disabled={location.isPending}
                  onClick={() => location.mutate(source.source_id)}
                >
                  Show map location
                </button>
              </li>
            ))}
          </ul>
          {location.error instanceof ApiError && (
            <p role="alert" className="error-banner">
              {location.error.message}
            </p>
          )}
          {mappedSource && <SourceLocationMap location={mappedSource} />}
        </div>

        <div className="panel">
          <h3>Register recorded-video source</h3>
          <form onSubmit={submitRegister} className="stacked-form">
            <label>
              Source name
              <input
                value={name}
                maxLength={120}
                onChange={(event) => setName(event.target.value)}
                placeholder="Entrance camera archive"
                required
              />
            </label>
            <label>
              Retention (days)
              <input
                type="number"
                min={1}
                max={30}
                value={retention}
                onChange={(event) => setRetention(Number(event.target.value))}
              />
            </label>
            <button type="submit" disabled={register.isPending || name.trim().length === 0}>
              {register.isPending ? "Registering…" : "Register source"}
            </button>
            {registerError && (
              <p role="alert" className="error-banner">
                {registerError.code === "role_forbidden"
                  ? "Registering sources requires the tenant admin role."
                  : `Registration failed (${registerError.code}): ${registerError.message}`}
              </p>
            )}
            {register.isSuccess && <p className="ok-banner">Source registered.</p>}
          </form>
        </div>

        <div className="panel">
          <h3>Upload recording</h3>
          <p className="muted small">
            Uploads are processed by Reka Vision to propose candidate detections for human
            review. Recordings are retained only for the configured retention period, then
            deleted along with derived remote assets.
          </p>
          <label className="file-drop">
            <input
              type="file"
              accept="video/mp4,.mp4"
              onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
            />
            {file ? `${file.name} (${(file.size / 1_048_576).toFixed(1)} MB)` : "Choose an MP4 recording"}
          </label>
          <label>
            Recorded source
            <select
              value={uploadSourceId}
              onChange={(event) => setUploadSourceId(event.target.value)}
            >
              <option value="">Select a registered recorded source</option>
              {sources.data?.items
                .filter((source) => source.mode === "recorded_video")
                .map((source) => (
                  <option key={source.source_id} value={source.source_id}>
                    {source.name}
                  </option>
                ))}
            </select>
          </label>
          <label className="row">
            <input
              type="checkbox"
              checked={consentConfirmed}
              onChange={(event) => setConsentConfirmed(event.target.checked)}
            />
            I confirm this tenant is authorized to process this recording.
          </label>
          {fileError && (
            <p role="alert" className="error-banner">
              {fileError}
            </p>
          )}
          <div className="row">
            <button
              type="button"
              disabled={!file || !uploadSourceId || !consentConfirmed || upload.isPending}
              onClick={() => upload.mutate()}
            >
              {upload.isPending ? "Requesting upload…" : "Start upload"}
            </button>
            {upload.isPending && (
              <button type="button" className="ghost" onClick={() => upload.reset()}>
                Cancel
              </button>
            )}
          </div>
          {uploadError && (
            <div role="alert" className="error-banner">
              {uploadError.code === "video_service_unavailable" ? (
                <>
                  <p>
                    The media-intake service is not connected in this environment, so the
                    upload could not start. Nothing was transferred.
                  </p>
                  <button type="button" onClick={() => upload.mutate()}>
                    Retry
                  </button>
                </>
              ) : (
                <p>
                  Upload failed ({uploadError.code}): {uploadError.message}
                </p>
              )}
            </div>
          )}
          {upload.isSuccess && (
            <p className="ok-banner">
              Upload accepted. Processing run {upload.data.run_id.slice(0, 8)} is {upload.data.state}.
            </p>
          )}
        </div>
      </div>
      <NearLiveReview />
    </section>
  );
}
