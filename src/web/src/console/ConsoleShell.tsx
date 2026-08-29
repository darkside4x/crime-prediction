import { useState } from "react";
import { useAuth } from "./AuthContext";
import { consoleRoute, navigate, useHashRoute, type ConsoleRoute } from "./router";
import SignIn from "./SignIn";
import ForecastView from "./ForecastView";
import SourcesView from "./SourcesView";
import ProcessingView from "./ProcessingView";
import ReviewView from "./ReviewView";
import ModelCardView from "./ModelCardView";

const NAV: Array<{ route: ConsoleRoute; label: string; minRole: "viewer" | "reviewer" | "tenant_admin" }> = [
  { route: "map", label: "Forecast map", minRole: "viewer" },
  { route: "review", label: "Review queue", minRole: "reviewer" },
  { route: "sources", label: "Sources & upload", minRole: "tenant_admin" },
  { route: "processing", label: "Processing & coverage", minRole: "viewer" },
  { route: "model-card", label: "Model card", minRole: "viewer" },
];

const ROLE_RANK = { viewer: 0, reviewer: 1, tenant_admin: 2, platform_operator: 3 } as const;

export default function ConsoleShell() {
  const { session, signOut, switchTenant } = useAuth();
  const hash = useHashRoute();
  const route = consoleRoute(hash) ?? "map";
  const [switching, setSwitching] = useState(false);

  if (!session) return <SignIn />;

  const rank = ROLE_RANK[session.role];
  const allowed = NAV.filter((item) => rank >= ROLE_RANK[item.minRole]);
  const activeMembership = session.memberships.find(
    (item) => item.tenant_id === session.activeTenantId,
  );

  const onTenantChange = async (tenantId: string) => {
    if (tenantId === session.activeTenantId) return;
    setSwitching(true);
    try {
      await switchTenant(tenantId);
      navigate("#/console/map");
    } finally {
      setSwitching(false);
    }
  };

  const currentAllowed = allowed.some((item) => item.route === route);

  return (
    <div className="console">
      <header className="console-bar">
        <a className="nav-logo" href="#/">
          HOT<span>SPOT</span>
        </a>
        <nav aria-label="Console">
          {allowed.map((item) => (
            <a
              key={item.route}
              href={`#/console/${item.route}`}
              aria-current={route === item.route ? "page" : undefined}
              className={route === item.route ? "active" : ""}
            >
              {item.label}
            </a>
          ))}
        </nav>
        <div className="console-identity">
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
          <button type="button" className="ghost" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>

      <p className="use-banner" role="note">
        Aggregate area-level forecasts with uncertainty — not ground truth. Prohibited:
        individual assessment, suspect identification, or automated enforcement.
      </p>

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
        ) : route === "map" ? (
          <ForecastView />
        ) : route === "sources" ? (
          <SourcesView />
        ) : route === "processing" ? (
          <ProcessingView />
        ) : route === "review" ? (
          <ReviewView />
        ) : (
          <ModelCardView />
        )}
      </main>
    </div>
  );
}
