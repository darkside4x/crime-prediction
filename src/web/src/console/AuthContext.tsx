/**
 * Development authentication context.
 *
 * Tokens are opaque bearer credentials resolved server-side; the client never
 * decides tenancy. Switching tenants goes through the API and clears every
 * cached tenant-scoped query so tenant A state can never leak into tenant B.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type MeTenants, type Role, type TenantMembership } from "../api/client";

export interface Session {
  token: string;
  principalLabel: string;
  activeTenantId: string;
  role: Role;
  memberships: TenantMembership[];
}

interface AuthValue {
  session: Session | null;
  authError: string | null;
  expired: boolean;
  signIn: (token: string, label: string) => Promise<void>;
  signOut: () => void;
  switchTenant: (tenantId: string) => Promise<void>;
  markExpired: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export const DEV_PERSONAS = [
  { label: "Tenant admin · Demo One", token: "demo-token-one" },
  { label: "Reviewer · Demo One", token: "demo-reviewer-one" },
  { label: "Viewer · Demo One", token: "demo-viewer-one" },
  { label: "Viewer · Demo Two", token: "demo-token-two" },
] as const;

function roleFor(me: MeTenants): Role {
  const active = me.tenants.find((item) => item.tenant_id === me.active_tenant_id);
  return active?.role ?? "viewer";
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<Session | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);

  const signIn = useCallback(
    async (token: string, label: string) => {
      setAuthError(null);
      setExpired(false);
      try {
        const me = await api.meTenants(token);
        queryClient.clear();
        setSession({
          token,
          principalLabel: label,
          activeTenantId: me.active_tenant_id,
          role: roleFor(me),
          memberships: me.tenants,
        });
      } catch (error) {
        if (error instanceof ApiError) {
          if (error.code === "expired_token") setExpired(true);
          setAuthError(
            error.code === "missing_token" || error.code === "invalid_token"
              ? "That token was not accepted. Use one of the development personas."
              : error.message,
          );
        } else {
          setAuthError("Could not reach the API. Is the backend running?");
        }
        throw error;
      }
    },
    [queryClient],
  );

  const signOut = useCallback(() => {
    queryClient.clear();
    setSession(null);
    setExpired(false);
    setAuthError(null);
  }, [queryClient]);

  const switchTenant = useCallback(
    async (tenantId: string) => {
      if (!session) return;
      const result = await api.switchTenant(session.token, tenantId);
      // Clear ALL cached queries and selections tied to the previous tenant.
      queryClient.clear();
      setSession({
        ...session,
        activeTenantId: result.active_tenant_id,
        role: result.role,
      });
    },
    [queryClient, session],
  );

  const markExpired = useCallback(() => {
    setExpired(true);
    setSession(null);
    queryClient.clear();
  }, [queryClient]);

  const value = useMemo(
    () => ({ session, authError, expired, signIn, signOut, switchTenant, markExpired }),
    [session, authError, expired, signIn, signOut, switchTenant, markExpired],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
