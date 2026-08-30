import { useEffect, useRef, useState, type FormEvent } from "react";
import { DEV_PERSONAS, useAuth } from "./AuthContext";
import {
  beginCognitoSignIn,
  cognitoConfig,
  consumeCognitoCallback,
} from "./cognito";

/** Development sign-in: opaque bearer token, resolved entirely server-side. */
export default function SignIn() {
  const { signIn, authError, expired } = useAuth();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [oauthError, setOauthError] = useState<string | null>(null);
  const consumedCallback = useRef(false);
  const hostedLogin = cognitoConfig() !== null;

  useEffect(() => {
    if (!hostedLogin || consumedCallback.current) return;
    consumedCallback.current = true;
    setBusy(true);
    void consumeCognitoCallback()
      .then(async (callback) => {
        if (callback) await signIn(callback.token, "Cognito account");
      })
      .catch(() => setOauthError("Secure sign-in could not be completed. Please try again."))
      .finally(() => setBusy(false));
  }, [hostedLogin, signIn]);

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
      <p className="eyebrow">Xecrex secure console</p>
      <h1 className="section-title">
        Sign <span className="accent">in</span>
      </h1>
      <p className="muted">
        {hostedLogin
          ? "Continue through the managed sign-in page. Roles and tenant access come from signed server-validated claims."
          : "Development authentication. Pick a persona or paste a bearer token; roles and tenant access are resolved by the server, never the browser."}
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
      {oauthError && (
        <p role="alert" className="error-banner">
          {oauthError}
        </p>
      )}
      {hostedLogin ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            setOauthError(null);
            void beginCognitoSignIn().catch(() => {
              setBusy(false);
              setOauthError("Secure sign-in is temporarily unavailable.");
            });
          }}
        >
          {busy ? "Opening secure sign-in…" : "Continue with secure sign-in"}
        </button>
      ) : (
        <>
          <div className="persona-groups">
            {["Demo One", "Demo Two"].map((tenant) => (
              <section className="persona-group" key={tenant}>
                <p className="persona-group-title">Demo Tenant {tenant.split(" ")[1]}</p>
                <div className="persona-grid">
                  {DEV_PERSONAS.filter((persona) => persona.label.endsWith(tenant)).map(
                    (persona) => (
                      <button
                        key={persona.token}
                        type="button"
                        className="persona-card"
                        disabled={busy}
                        aria-label={persona.label}
                        onClick={(event) => submit(event, persona.token, persona.label)}
                      >
                        {persona.label.split(" · ")[0]}
                      </button>
                    ),
                  )}
                </div>
              </section>
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
        </>
      )}
    </div>
  );
}
