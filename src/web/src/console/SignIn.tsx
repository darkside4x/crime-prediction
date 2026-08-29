import { useState, type FormEvent } from "react";
import { DEV_PERSONAS, useAuth } from "./AuthContext";

/** Development sign-in: opaque bearer token, resolved entirely server-side. */
export default function SignIn() {
  const { signIn, authError, expired } = useAuth();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent, value: string, label: string) => {
    event.preventDefault();
    if (!value || busy) return;
    setBusy(true);
    try {
      await signIn(value, label);
    } catch {
      /* surfaced via authError */
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="console-signin">
      <h1 className="section-title">
        SIGN <span className="accent">IN</span>
      </h1>
      <p className="muted">
        Development authentication. Pick a persona or paste a bearer token; roles and
        tenant access are resolved by the server, never the browser.
      </p>
      {expired && (
        <p role="alert" className="error-banner">
          Your session expired. Sign in again to continue.
        </p>
      )}
      {authError && !expired && (
        <p role="alert" className="error-banner">
          {authError}
        </p>
      )}
      <div className="persona-grid">
        {DEV_PERSONAS.map((persona) => (
          <button
            key={persona.token}
            type="button"
            className="persona-card"
            disabled={busy}
            onClick={(event) => submit(event, persona.token, persona.label)}
          >
            {persona.label}
          </button>
        ))}
      </div>
      <form onSubmit={(event) => submit(event, token, "Custom token")} className="token-form">
        <label htmlFor="token-input">Bearer token</label>
        <input
          id="token-input"
          type="password"
          autoComplete="off"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="demo-token-one"
        />
        <button type="submit" disabled={busy || token.length === 0}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
