import { lazy, Suspense, useState } from "react";
import { DEV_PERSONAS, useAuth } from "./AuthContext";
import { consoleRoute, navigate, useHashRoute, type ConsoleRoute } from "./router";
import SignIn from "./SignIn";

const ForecastView = lazy(() => import("./ForecastView"));
const LiveOperations = lazy(() => import("../components/NearLiveReview"));
const SourcesView = lazy(() => import("./SourcesView"));
const ProcessingView = lazy(() => import("./ProcessingView"));
const ReviewView = lazy(() => import("./ReviewView"));
const ResponseDirectoryView = lazy(() => import("./ResponseDirectoryView"));
const MobileCaptureView = lazy(() => import("./MobileCaptureView"));
const ModelCardView = lazy(() => import("./ModelCardView"));

const NAV: Array<{ route: ConsoleRoute; label: string; minRole: "viewer" | "reviewer" | "tenant_admin" }> = [
  { route: "live", label: "Live", minRole: "tenant_admin" },
  { route: "review", label: "Review", minRole: "reviewer" },
  { route: "map", label: "Prediction", minRole: "viewer" },
  { route: "response", label: "Response", minRole: "tenant_admin" },
  { route: "sources", label: "Sources", minRole: "tenant_admin" },
  { route: "mobile-capture", label: "Capture", minRole: "tenant_admin" },
  { route: "processing", label: "System", minRole: "viewer" },
  { route: "model-card", label: "Model", minRole: "viewer" },
];

const ROUTE_CONTEXT: Record<ConsoleRoute, { title: string; code: string }> = {
  live: { title: "Live monitor", code: "REKA / VISION" },
  review: { title: "Review queue", code: "HUMAN / REVIEW" },
  map: { title: "Prediction map", code: "H3 / 06H" },
  response: { title: "Response directory", code: "VOICE / POC" },
  sources: { title: "Sources & upload", code: "MEDIA / INTAKE" },
  "mobile-capture": { title: "Mobile capture", code: "PHONE / CAMERA" },
  processing: { title: "System status", code: "NODE / ALPHA" },
  "model-card": { title: "Model card", code: "MODEL / DOSSIER" },
};

function RouteGlyph({ route }: { route: ConsoleRoute }) {
  if (route === "map") {
    return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Z"/><path d="M9 3v15m6-12v15"/></svg>;
  }
  if (route === "live") {
    return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h13v12H3z"/><path d="m16 10 5-3v10l-5-3z"/></svg>;
  }
  if (route === "review") {
    return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h12v18H6z"/><path d="m9 12 2 2 4-5M9 6h6m-6 12h6"/></svg>;
  }
  if (route === "processing") {
    return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v4m0 12v4M2 12h4m12 0h4M5 5l3 3m8 8 3 3M19 5l-3 3M8 16l-3 3"/><circle cx="12" cy="12" r="4"/></svg>;
  }
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h11l3 3v15H5z"/><path d="M9 9h6m-6 4h6m-6 4h4"/></svg>;
}

const ROLE_RANK = { viewer: 0, reviewer: 1, tenant_admin: 2, platform_operator: 3 } as const;

export default function ConsoleShell() {
  const { session, signIn, signOut, switchTenant } = useAuth();
  const hash = useHashRoute();
  const requestedRoute = consoleRoute(hash) ?? "live";
  const [switching, setSwitching] = useState(false);

  if (!session) return <SignIn />;

  const rank = ROLE_RANK[session.role];
  const allowed = NAV.filter((item) => rank >= ROLE_RANK[item.minRole]);
  const defaultRoute: ConsoleRoute = session.role === "tenant_admin" || session.role === "platform_operator"
    ? "live"
    : session.role === "reviewer"
      ? "review"
      : "map";
  const route = hash === "#/console" || hash === "#/console/" ? defaultRoute : requestedRoute;
  const activeMembership = session.memberships.find(
    (item) => item.tenant_id === session.activeTenantId,
  );

  const onTenantChange = async (tenantId: string) => {
    if (tenantId === session.activeTenantId) return;
    setSwitching(true);
    try {
      await switchTenant(tenantId);
      navigate("#/console");
    } finally {
      setSwitching(false);
    }
  };

  const onPersonaChange = async (token: string) => {
    const persona = DEV_PERSONAS.find((item) => item.token === token);
    if (!persona || token === session.token) return;
    setSwitching(true);
    try {
      await signIn(persona.token, persona.label);
      navigate("#/console");
    } finally {
      setSwitching(false);
    }
  };

  const currentAllowed = allowed.some((item) => item.route === route);

  const routeContext = ROUTE_CONTEXT[route];
  const tenantName = activeMembership?.display_name ?? "Active tenant";
  const initials = session.principalLabel
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");

  return (
    <div className={`console console-${route}`}>
      <aside className="console-sidebar">
        <div className="console-brand">
          <a href="#/">Xecrex</a>
          <span>Console v2.4</span>
          <small>High integrity mode</small>
        </div>

        <div className="console-operator">
          <span className="operator-mark" aria-hidden="true">{initials || "OP"}</span>
          <span><strong>{tenantName}</strong><small>{session.principalLabel}</small></span>
        </div>
        <nav aria-label="Console">
          {allowed.map((item) => (
            <a
              key={item.route}
              href={`#/console/${item.route}`}
              aria-current={route === item.route ? "page" : undefined}
              className={route === item.route ? "active" : ""}
            >
              <span className="console-nav-icon"><RouteGlyph route={item.route} /></span>
              <span>{item.label}</span>
            </a>
          ))}
        </nav>

        <div className="console-sidebar-foot">
          <button type="button" onClick={signOut}>Sign out</button>
          <p><i /> API connected</p>
          <p><b /> Worker active</p>
        </div>
      </aside>

      <section className="console-canvas">
        <header className="console-bar">
          <div className="console-route-title">
            <h1>{routeContext.title}</h1>
            <span>{routeContext.code}</span>
          </div>
        <div className="console-identity">
          {DEV_PERSONAS.length > 0 && (
            <>
              <label className="visually-hidden" htmlFor="persona-select">Demo persona</label>
              <select
                id="persona-select"
                value={session.token}
                disabled={switching}
                onChange={(event) => void onPersonaChange(event.target.value)}
              >
                {DEV_PERSONAS.map((persona) => (
                  <option key={persona.token} value={persona.token}>{persona.label}</option>
                ))}
              </select>
            </>
          )}
          <label className="visually-hidden" htmlFor="tenant-select">
            Active tenant
          </label>
          <select
            id="tenant-select"
            value={session.activeTenantId}
            disabled={switching}
            onChange={(event) => void onTenantChange(event.target.value)}
          >
            {session.memberships.map((membership) => (
              <option key={membership.tenant_id} value={membership.tenant_id}>
                {membership.display_name}
              </option>
            ))}
          </select>
          <span className="role-badge" title="Active role in this tenant">
            {activeMembership?.role ?? session.role}
          </span>
        </div>
        </header>

        <main className="console-main">
        {switching ? (
          <p className="muted">Switching tenant…</p>
        ) : !currentAllowed ? (
          <div className="forbidden" role="alert">
            <h2>Not available for your role</h2>
            <p>
              Your role in {activeMembership?.display_name ?? "this tenant"} is{" "}
              <strong>{session.role}</strong>. This area requires elevated access.
            </p>
          </div>
        ) : (
          <Suspense fallback={<p className="muted">Loading tenant view…</p>}>
            {route === "live" ? (
              <LiveOperations key={session.activeTenantId} />
            ) : route === "map" ? (
              <ForecastView key={session.activeTenantId} />
            ) : route === "sources" ? (
              <SourcesView key={session.activeTenantId} />
            ) : route === "processing" ? (
              <ProcessingView key={session.activeTenantId} />
            ) : route === "review" ? (
              <ReviewView key={session.activeTenantId} />
            ) : route === "response" ? (
              <ResponseDirectoryView key={session.activeTenantId} />
            ) : route === "mobile-capture" ? (
              <MobileCaptureView key={session.activeTenantId} />
            ) : (
              <ModelCardView key={session.activeTenantId} />
            )}
          </Suspense>
        )}
        </main>
        <p className="use-banner" role="note">
          Area-level estimates with uncertainty · human review required · no identity assessment or automated enforcement
        </p>
      </section>
    </div>
  );
}
