const PKCE_VERIFIER = "crime_oauth_pkce_verifier";
const OAUTH_STATE = "crime_oauth_state";
const RETURN_HASH = "crime_oauth_return_hash";

interface CognitoConfig {
  domain: string;
  clientId: string;
  redirectUri: string;
  logoutUri: string;
}

export interface CognitoCallback {
  token: string;
  returnHash: string;
}

function configuredValue(value: string | undefined): string {
  return value?.trim() ?? "";
}

export function cognitoConfig(): CognitoConfig | null {
  const domain = configuredValue(import.meta.env.VITE_COGNITO_DOMAIN).replace(/\/$/, "");
  const clientId = configuredValue(import.meta.env.VITE_COGNITO_CLIENT_ID);
  if (!domain || !clientId) return null;
  if (!domain.startsWith("https://")) throw new Error("Cognito domain must use HTTPS");
  return {
    domain,
    clientId,
    redirectUri:
      configuredValue(import.meta.env.VITE_COGNITO_REDIRECT_URI) ||
      `${window.location.origin}${window.location.pathname}`,
    logoutUri:
      configuredValue(import.meta.env.VITE_COGNITO_LOGOUT_URI) ||
      `${window.location.origin}${window.location.pathname}`,
  };
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomValue(length = 32): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(length)));
}

export async function beginCognitoSignIn(): Promise<void> {
  const config = cognitoConfig();
  if (!config) throw new Error("Cognito is not configured");
  const verifier = randomValue(48);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const state = randomValue();
  sessionStorage.setItem(PKCE_VERIFIER, verifier);
  sessionStorage.setItem(OAUTH_STATE, state);
  sessionStorage.setItem(RETURN_HASH, window.location.hash || "#/console");
  const query = new URLSearchParams({
    client_id: config.clientId,
    response_type: "code",
    scope: "openid",
    redirect_uri: config.redirectUri,
    state,
    code_challenge_method: "S256",
    code_challenge: base64Url(new Uint8Array(digest)),
  });
  window.location.assign(`${config.domain}/oauth2/authorize?${query}`);
}

export async function consumeCognitoCallback(): Promise<CognitoCallback | null> {
  const config = cognitoConfig();
  const query = new URLSearchParams(window.location.search);
  const code = query.get("code");
  const returnedState = query.get("state");
  const oauthError = query.get("error_description") || query.get("error");
  if (!code && !oauthError) return null;
  if (!config) throw new Error("Cognito callback received without configuration");
  if (oauthError) throw new Error("Cognito sign-in was not completed");
  const expectedState = sessionStorage.getItem(OAUTH_STATE);
  const verifier = sessionStorage.getItem(PKCE_VERIFIER);
  if (!returnedState || returnedState !== expectedState || !verifier) {
    throw new Error("Cognito sign-in state was invalid or expired");
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: config.clientId,
    code: code!,
    redirect_uri: config.redirectUri,
    code_verifier: verifier,
  });
  const response = await fetch(`${config.domain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const result = (await response.json()) as { id_token?: unknown };
  if (!response.ok || typeof result.id_token !== "string") {
    throw new Error("Cognito token exchange failed");
  }
  const storedReturnHash = sessionStorage.getItem(RETURN_HASH);
  const returnHash = storedReturnHash?.startsWith("#/console")
    ? storedReturnHash
    : "#/console";
  sessionStorage.removeItem(PKCE_VERIFIER);
  sessionStorage.removeItem(OAUTH_STATE);
  sessionStorage.removeItem(RETURN_HASH);
  const oldURL = window.location.href;
  window.history.replaceState(null, "", `${window.location.pathname}${returnHash}`);
  // replaceState does not emit hashchange. Notify every mounted router so an
  // OAuth callback cannot leave the URL and rendered console view out of sync.
  window.dispatchEvent(
    new HashChangeEvent("hashchange", { oldURL, newURL: window.location.href }),
  );
  return { token: result.id_token, returnHash };
}

export function cognitoLogoutUrl(): string | null {
  const config = cognitoConfig();
  if (!config) return null;
  const query = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: config.logoutUri,
  });
  return `${config.domain}/logout?${query}`;
}
