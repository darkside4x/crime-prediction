import { useMemo, useState } from "react";
import "./MobileCaptureShare.css";

export default function MobileCaptureShare() {
  const [feedback, setFeedback] = useState("");
  const mobileUrl = useMemo(
    () => `${window.location.origin}${window.location.pathname}#/console/mobile-capture`,
    [],
  );

  const shareOrCopy = async () => {
    setFeedback("");
    try {
      if (typeof navigator.share === "function") {
        await navigator.share({
          title: "CivicHalo mobile capture",
          text: "Open the authenticated CivicHalo mobile capture page.",
          url: mobileUrl,
        });
        setFeedback("Mobile capture link shared.");
        return;
      }
      await navigator.clipboard.writeText(mobileUrl);
      setFeedback("Mobile capture link copied.");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        setFeedback("Sharing canceled.");
      } else {
        setFeedback("Copy this clean route from the field below.");
      }
    }
  };

  return (
    <div className="panel mobile-capture-share">
      <h3>Use this phone as an approved camera</h3>
      <p className="muted small">
        Open the authenticated mobile route, select a registered source and capture one
        bounded clip. The link contains no bearer token or tenant identifier.
      </p>
      <label>
        Mobile capture link
        <input value={mobileUrl} readOnly onFocus={(event) => event.currentTarget.select()} />
      </label>
      <div className="row">
        <a className="mobile-capture-open" href="#/console/mobile-capture">
          Open on this device
        </a>
        <button type="button" className="ghost" onClick={() => void shareOrCopy()}>
          Share or copy link
        </button>
      </div>
      {feedback ? (
        <p className="muted small" role="status" aria-live="polite">
          {feedback}
        </p>
      ) : null}
    </div>
  );
}
