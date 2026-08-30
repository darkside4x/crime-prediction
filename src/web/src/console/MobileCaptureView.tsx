import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError, newIdempotencyKey } from "../api/client";
import { useAuth } from "./AuthContext";
import "./MobileCaptureView.css";

const CAPTURE_DURATIONS = [10, 15, 20] as const;
const PERMISSION_SLOW_MS = 8_000;
const UPLOAD_SLOW_MS = 8_000;
const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
const CAPTURE_MIME_TYPES = [
  "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
  "video/mp4;codecs=avc1.42E01E",
  "video/mp4",
  "video/webm;codecs=vp9,opus",
  "video/webm;codecs=vp8,opus",
  "video/webm;codecs=vp8",
  "video/webm",
] as const;

type CaptureDuration = (typeof CAPTURE_DURATIONS)[number];
type CapturePhase =
  | "idle"
  | "requesting_permission"
  | "ready"
  | "recording"
  | "recorded"
  | "uploading"
  | "uploaded"
  | "permission_denied"
  | "camera_unavailable"
  | "unsupported"
  | "clip_too_large"
  | "interrupted"
  | "upload_interrupted"
  | "upload_failed";

interface CameraChoice {
  deviceId: string;
  label: string;
}

interface CapturedClip {
  file: File;
  capturedStart: string;
  capturedEnd: string;
}

function acceptedCaptureMimeType(): string | null {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") {
    return null;
  }
  return CAPTURE_MIME_TYPES.find((mimeType) => MediaRecorder.isTypeSupported(mimeType)) ?? null;
}

function captureFormat(mimeType: string): { contentType: "video/mp4" | "video/webm"; extension: "mp4" | "webm"; label: "MP4" | "WebM" } {
  return mimeType.startsWith("video/mp4")
    ? { contentType: "video/mp4", extension: "mp4", label: "MP4" }
    : { contentType: "video/webm", extension: "webm", label: "WebM" };
}

function cameraErrorPhase(error: unknown): CapturePhase {
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "permission_denied";
    }
    if (error.name === "NotFoundError" || error.name === "OverconstrainedError") {
      return "camera_unavailable";
    }
  }
  return "interrupted";
}

function phaseMessage(phase: CapturePhase): string {
  switch (phase) {
    case "requesting_permission":
      return "Waiting for camera permission.";
    case "ready":
      return "Camera ready. Nothing is recorded until you press Start recording.";
    case "recording":
      return "Recording stays on this device until the bounded clip is complete.";
    case "recorded":
      return "Clip ready. Review the authorization statement before uploading.";
    case "uploading":
      return "Uploading the bounded clip to the authenticated CivicHalo backend.";
    case "uploaded":
      return "Upload accepted. The backend will send it through the normal review pipeline.";
    case "permission_denied":
      return "Camera permission was blocked. Allow camera access in browser settings, then try again.";
    case "camera_unavailable":
      return "No usable camera was found. Check the selected camera or close other camera apps.";
    case "unsupported":
      return "This browser cannot create an MP4 or WebM clip accepted by the secure media intake service.";
    case "clip_too_large":
      return "The clip exceeded the secure 8 MB gateway limit. Discard it and record a shorter clip.";
    case "interrupted":
      return "Camera capture was interrupted. The incomplete clip was discarded and was not uploaded.";
    case "upload_interrupted":
      return "The network connection ended before upload confirmation. Retry uses the same idempotency key.";
    case "upload_failed":
      return "The backend rejected the upload. Nothing was sent directly to Reka from this browser.";
    default:
      return "Camera is off. Enabling preview does not start recording.";
  }
}

export default function MobileCaptureView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const captureStartRef = useRef<number | null>(null);
  const recordingTimerRef = useRef<number | null>(null);
  const countdownTimerRef = useRef<number | null>(null);
  const permissionTimerRef = useRef<number | null>(null);
  const cameraRequestRef = useRef(0);
  const mountedRef = useRef(true);
  const discardRecordingRef = useRef(false);
  const interruptedRecordingRef = useRef(false);
  const uploadKeyRef = useRef(newIdempotencyKey());

  const [phase, setPhase] = useState<CapturePhase>("idle");
  const [duration, setDuration] = useState<CaptureDuration>(15);
  const [remainingSeconds, setRemainingSeconds] = useState<number>(15);
  const [sourceId, setSourceId] = useState("");
  const [consentConfirmed, setConsentConfirmed] = useState(false);
  const [cameras, setCameras] = useState<CameraChoice[]>([]);
  const [selectedCameraId, setSelectedCameraId] = useState("");
  const [clip, setClip] = useState<CapturedClip | null>(null);
  const [permissionSlow, setPermissionSlow] = useState(false);
  const [uploadSlow, setUploadSlow] = useState(false);

  const secureCameraContext =
    typeof window !== "undefined" &&
    window.isSecureContext &&
    typeof navigator.mediaDevices?.getUserMedia === "function";
  const captureMimeType = useMemo(acceptedCaptureMimeType, []);
  const cameraSupported = secureCameraContext && captureMimeType !== null;

  const sources = useQuery({
    queryKey: ["sources", tenantId],
    queryFn: () => api.sources(token),
  });
  const recordedSources =
    sources.data?.items.filter((source) => source.mode === "recorded_video") ?? [];

  const clearRecordingTimers = () => {
    if (recordingTimerRef.current !== null) window.clearTimeout(recordingTimerRef.current);
    if (countdownTimerRef.current !== null) window.clearInterval(countdownTimerRef.current);
    recordingTimerRef.current = null;
    countdownTimerRef.current = null;
  };

  const stopStream = () => {
    for (const track of streamRef.current?.getTracks() ?? []) track.stop();
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
  };

  const discardRecording = (nextPhase: CapturePhase = "ready") => {
    discardRecordingRef.current = true;
    interruptedRecordingRef.current = nextPhase === "interrupted";
    clearRecordingTimers();
    if (recorderRef.current?.state === "recording") {
      recorderRef.current.stop();
    }
    chunksRef.current = [];
    captureStartRef.current = null;
    setClip(null);
    setRemainingSeconds(duration);
    setPhase(nextPhase);
  };

  const handleStreamEnded = () => {
    if (recorderRef.current?.state === "recording") {
      discardRecording("interrupted");
    } else {
      setPhase("interrupted");
    }
  };

  const openCamera = async (deviceId?: string) => {
    if (!cameraSupported) {
      setPhase("unsupported");
      return;
    }
    setPhase("requesting_permission");
    const requestNumber = ++cameraRequestRef.current;
    setPermissionSlow(false);
    permissionTimerRef.current = window.setTimeout(
      () => setPermissionSlow(true),
      PERMISSION_SLOW_MS,
    );
    stopStream();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: deviceId
          ? { deviceId: { exact: deviceId } }
          : { facingMode: { ideal: "environment" } },
      });
      if (!mountedRef.current || requestNumber !== cameraRequestRef.current) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      streamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;
      for (const track of stream.getVideoTracks()) {
        track.addEventListener("ended", handleStreamEnded, { once: true });
      }
      const devices = await navigator.mediaDevices.enumerateDevices();
      const choices = devices
        .filter((device) => device.kind === "videoinput")
        .map((device, index) => ({
          deviceId: device.deviceId,
          label: device.label || `Camera ${index + 1}`,
        }));
      setCameras(choices);
      const activeDevice = stream.getVideoTracks()[0]?.getSettings().deviceId;
      setSelectedCameraId(activeDevice ?? deviceId ?? choices[0]?.deviceId ?? "");
      setPhase("ready");
    } catch (error) {
      if (!mountedRef.current || requestNumber !== cameraRequestRef.current) return;
      stopStream();
      setPhase(cameraErrorPhase(error));
    } finally {
      if (permissionTimerRef.current !== null) window.clearTimeout(permissionTimerRef.current);
      permissionTimerRef.current = null;
      if (mountedRef.current && requestNumber === cameraRequestRef.current) {
        setPermissionSlow(false);
      }
    }
  };

  const startRecording = () => {
    const stream = streamRef.current;
    if (!stream || !sourceId || !captureMimeType) return;
    setClip(null);
    setConsentConfirmed(false);
    chunksRef.current = [];
    discardRecordingRef.current = false;
    interruptedRecordingRef.current = false;
    uploadKeyRef.current = newIdempotencyKey();
    const startedAt = Date.now();
    captureStartRef.current = startedAt;
    setRemainingSeconds(duration);

    try {
      const recorder = new MediaRecorder(stream, {
        mimeType: captureMimeType,
        videoBitsPerSecond: 1_200_000,
      });
      recorderRef.current = recorder;
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onerror = () => {
        discardRecording("interrupted");
      };
      recorder.onstop = () => {
        clearRecordingTimers();
        if (discardRecordingRef.current || interruptedRecordingRef.current) {
          chunksRef.current = [];
          return;
        }
        const stoppedAt = Date.now();
        const format = captureFormat(captureMimeType);
        const blob = new Blob(chunksRef.current, { type: format.contentType });
        chunksRef.current = [];
        if (blob.size === 0 || captureStartRef.current === null) {
          setPhase("interrupted");
          return;
        }
        const file = new File(
          [blob],
          `civichalo-mobile-${new Date(stoppedAt).toISOString().replaceAll(":", "-")}.${format.extension}`,
          { type: format.contentType, lastModified: stoppedAt },
        );
        setClip({
          file,
          capturedStart: new Date(captureStartRef.current).toISOString(),
          capturedEnd: new Date(stoppedAt).toISOString(),
        });
        setRemainingSeconds(0);
        setPhase(blob.size > MAX_UPLOAD_BYTES ? "clip_too_large" : "recorded");
      };
      recorder.start(1_000);
      setPhase("recording");
      countdownTimerRef.current = window.setInterval(() => {
        const elapsed = (Date.now() - startedAt) / 1_000;
        setRemainingSeconds(Math.max(0, Math.ceil(duration - elapsed)));
      }, 250);
      recordingTimerRef.current = window.setTimeout(() => {
        if (recorder.state === "recording") recorder.stop();
      }, duration * 1_000);
    } catch {
      setPhase("unsupported");
    }
  };

  const upload = useMutation({
    mutationFn: (captured: CapturedClip) =>
      api.uploadVideo(
        token,
        {
          sourceId,
          capturedStart: captured.capturedStart,
          capturedEnd: captured.capturedEnd,
          consentConfirmed,
          file: captured.file,
        },
        uploadKeyRef.current,
      ),
    onMutate: () => {
      setUploadSlow(false);
      setPhase("uploading");
    },
    onSuccess: () => {
      setPhase("uploaded");
    },
    onError: (error) => {
      setPhase(error instanceof ApiError ? "upload_failed" : "upload_interrupted");
    },
  });

  const processing = useQuery({
    queryKey: ["ingestion-run", upload.data?.run_id],
    queryFn: () => api.ingestionRun(token, upload.data!.run_id),
    enabled: Boolean(upload.data?.run_id),
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state === "completed" || state === "failed" ? false : 3_000;
    },
  });

  useEffect(() => {
    if (!upload.isPending) {
      setUploadSlow(false);
      return;
    }
    const timer = window.setTimeout(() => setUploadSlow(true), UPLOAD_SLOW_MS);
    return () => window.clearTimeout(timer);
  }, [upload.isPending]);

  useEffect(() => {
    setRemainingSeconds(duration);
  }, [duration]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cameraRequestRef.current += 1;
      if (permissionTimerRef.current !== null) window.clearTimeout(permissionTimerRef.current);
      clearRecordingTimers();
      discardRecordingRef.current = true;
      if (recorderRef.current?.state === "recording") recorderRef.current.stop();
      stopStream();
    };
  }, []);

  const uploadError = upload.error instanceof ApiError ? upload.error : null;
  const phaseIsError = [
    "permission_denied",
    "camera_unavailable",
    "unsupported",
    "clip_too_large",
    "interrupted",
    "upload_interrupted",
    "upload_failed",
  ].includes(phase);

  return (
    <section className="mobile-capture-view">
      <div className="mobile-capture-heading">
        <div>
          <p className="eyebrow">Authorized tenant capture</p>
          <h2 className="section-title">
            MOBILE <span className="accent">CAPTURE</span>
          </h2>
        </div>
        <a className="mobile-capture-back" href="#/console/sources">
          Back to sources
        </a>
      </div>

      <p className="mobile-capture-boundary" role="note">
        Capture one approved 10–20 second clip. CivicHalo does not request GPS, record in
        the background, or send media directly from this browser to Reka.
      </p>

      <div className="mobile-capture-layout">
        <div className="mobile-camera-card">
          <div className="mobile-camera-preview">
            <video ref={videoRef} autoPlay muted playsInline aria-label="Mobile camera preview" />
            {phase === "idle" || phase === "unsupported" ? (
              <div className="mobile-camera-placeholder" aria-hidden="true">
                <span>Camera off</span>
              </div>
            ) : null}
            {phase === "recording" ? (
              <div className="mobile-recording-indicator" aria-live="polite">
                <i aria-hidden="true" /> Recording · {remainingSeconds}s remaining
              </div>
            ) : null}
          </div>

          <div
            className={phaseIsError ? "mobile-capture-state is-error" : "mobile-capture-state"}
            role={phaseIsError ? "alert" : "status"}
            aria-live="polite"
          >
            <strong>{phase.replaceAll("_", " ")}</strong>
            <span>{phaseMessage(phase)}</span>
            {permissionSlow ? <span>Still waiting—check for a browser permission prompt.</span> : null}
            {uploadSlow ? <span>Network is slow. Keep this page open; do not submit again yet.</span> : null}
          </div>

          {!cameraSupported ? (
            <div className="error-banner" role="alert">
              {!secureCameraContext
                ? "Camera capture requires an HTTPS page (localhost is allowed for development)."
                : "This browser does not offer MP4 or WebM MediaRecorder output. Use a supported browser or upload an existing recording from Sources."}
            </div>
          ) : null}

          <div className="mobile-capture-actions">
            {(phase === "idle" ||
              phase === "permission_denied" ||
              phase === "camera_unavailable" ||
              phase === "interrupted") && (
              <button
                type="button"
                disabled={!cameraSupported}
                onClick={() => void openCamera(selectedCameraId || undefined)}
              >
                {phase === "idle" ? "Enable camera preview" : "Try camera again"}
              </button>
            )}
            {phase === "requesting_permission" ? (
              <button type="button" disabled>
                Requesting permission…
              </button>
            ) : null}
            {phase === "ready" ? (
              <button type="button" disabled={!sourceId} onClick={startRecording}>
                Start {duration}-second recording
              </button>
            ) : null}
            {phase === "recording" ? (
              <button
                type="button"
                className="ghost"
                onClick={() => discardRecording("ready")}
              >
                Stop and discard
              </button>
            ) : null}
            {(phase === "recorded" ||
              phase === "uploaded" ||
              phase === "clip_too_large" ||
              phase === "upload_failed" ||
              phase === "upload_interrupted") && (
              <button
                type="button"
                className="ghost"
                onClick={() => {
                  setClip(null);
                  setConsentConfirmed(false);
                  upload.reset();
                  uploadKeyRef.current = newIdempotencyKey();
                  setPhase("ready");
                }}
              >
                {phase === "uploaded" ? "Capture another clip" : "Discard and retake"}
              </button>
            )}
          </div>
        </div>

        <div className="panel mobile-capture-controls">
          <h3>Capture settings</h3>
          <label>
            Registered recorded source
            <select
              value={sourceId}
              disabled={phase === "recording" || upload.isPending || clip !== null}
              onChange={(event) => setSourceId(event.target.value)}
            >
              <option value="">Select the source/location for this phone</option>
              {recordedSources.map((source) => (
                <option key={source.source_id} value={source.source_id}>
                  {source.name}
                </option>
              ))}
            </select>
          </label>
          {sources.isLoading ? <p className="muted small">Loading registered sources…</p> : null}
          {sources.isError ? (
            <p className="error-banner" role="alert">
              Registered sources could not be loaded. Try again from Sources &amp; upload.
            </p>
          ) : null}
          {!sources.isLoading && !sources.isError && recordedSources.length === 0 ? (
            <p className="error-banner" role="alert">
              No recorded source is registered for this tenant. Register one before capture.
            </p>
          ) : null}
          <p className="muted small">
            The registered source supplies the approved broad location. Phone GPS is never
            requested.
          </p>

          <label>
            Camera
            <select
              value={selectedCameraId}
              disabled={
                cameras.length < 2 || phase === "recording" || upload.isPending || clip !== null
              }
              onChange={(event) => {
                const nextDevice = event.target.value;
                setSelectedCameraId(nextDevice);
                void openCamera(nextDevice);
              }}
            >
              {cameras.length === 0 ? <option value="">Available after permission</option> : null}
              {cameras.map((camera) => (
                <option key={camera.deviceId} value={camera.deviceId}>
                  {camera.label}
                </option>
              ))}
            </select>
          </label>

          <fieldset disabled={phase === "recording" || upload.isPending || clip !== null}>
            <legend>Clip length</legend>
            <div className="mobile-duration-options">
              {CAPTURE_DURATIONS.map((seconds) => (
                <label key={seconds}>
                  <input
                    type="radio"
                    name="capture-duration"
                    value={seconds}
                    checked={duration === seconds}
                    onChange={() => setDuration(seconds)}
                  />
                  {seconds}s
                </label>
              ))}
            </div>
          </fieldset>

          {clip ? (
            <div className="mobile-clip-summary">
              <strong>Bounded clip ready</strong>
              <span>
                {(clip.file.size / 1_048_576).toFixed(1)} MB · {captureFormat(clip.file.type).label}
                {clip.file.type === "video/webm" ? " · converted securely by the backend" : ""}
              </span>
            </div>
          ) : null}

          <label className="mobile-consent-row">
            <input
              type="checkbox"
              checked={consentConfirmed}
              disabled={!clip || upload.isPending || upload.isSuccess}
              onChange={(event) => setConsentConfirmed(event.target.checked)}
            />
            I confirm this tenant is authorized to process this clip under the registered
            source retention policy.
          </label>

          <button
            type="button"
            disabled={
              !clip ||
              clip.file.size > MAX_UPLOAD_BYTES ||
              !sourceId ||
              !consentConfirmed ||
              upload.isPending ||
              upload.isSuccess
            }
            onClick={() => clip && upload.mutate(clip)}
          >
            {upload.isPending ? "Uploading securely…" : "Upload for human review"}
          </button>
          {(phase === "upload_interrupted" || phase === "upload_failed") && clip ? (
            <button
              type="button"
              className="ghost"
              disabled={upload.isPending}
              onClick={() => upload.mutate(clip)}
            >
              Retry upload
            </button>
          ) : null}

          {uploadError ? (
            <p className="error-banner" role="alert">
              Upload failed ({uploadError.code}): {uploadError.message}
            </p>
          ) : null}

          {upload.data ? (
            <div className="ok-banner" aria-live="polite">
              <p>Upload accepted · run {upload.data.run_id.slice(0, 8)}.</p>
              {processing.isLoading ? <p>Queued for secure media processing…</p> : null}
              {processing.data?.state === "completed" ? (
                <p>
                  Analysis complete · {processing.data.candidate_count ?? 0} unconfirmed
                  candidate(s) ready for a human reviewer.
                </p>
              ) : null}
              {processing.data && processing.data.state !== "completed" ? (
                <p>
                  {processing.data.state === "failed" ? "Processing failed" : "Processing"} ·{" "}
                  {processing.data.stage.replaceAll("_", " ")}
                </p>
              ) : null}
              {processing.isError ? <p>Processing status could not be refreshed.</p> : null}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
