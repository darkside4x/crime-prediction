/** Typed client for the tenant-aware FastAPI surface. */

export interface TenantInfo {
  tenant_id: string;
  slug: string;
  display_name: string;
  role: string;
}

export interface WindowOption {
  window_start: string;
  window_end: string;
}

export interface Metadata {
  categories: string[];
  windows: WindowOption[];
  h3_resolution: number;
  model_version: string;
  data_version: string;
  data_as_of: string;
  limitations: string[];
}

export interface Uncertainty {
  lower: number;
  upper: number;
}

export interface Driver {
  feature: string;
  direction: "higher" | "lower";
}

export interface CellProperties {
  cell_id: string;
  window_start: string;
  window_end: string;
  category: string;
  suppressed: boolean;
  risk?: number;
  risk_band?: "typical" | "moderate" | "elevated";
  expected_count?: number;
  uncertainty?: Uncertainty;
  drivers?: Driver[];
  model_version: string;
  data_version: string;
  data_as_of: string;
}

export interface RiskCollection {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    id: string;
    geometry: { type: "Polygon"; coordinates: number[][][] };
    properties: CellProperties;
  }>;
  model_version: string;
  data_version: string;
  data_as_of: string;
}

export interface CellExplanation {
  prediction: CellProperties & { tenant_id?: string };
  recent_trend: Array<{ date: string; count: number }>;
  limitations: string[];
}

export interface SourceInfo {
  source_id: string;
  name: string;
  kind: string;
  status: string;
  freshness: {
    last_accepted_event_at: string;
    last_received_at: string;
    lag_seconds: number;
    rejected_count: number;
  };
}

export interface CopilotInsight {
  request_id: string;
  answer: string;
  claims: Array<{ text: string; fact_ids: string[] }>;
  limitations: string[];
  data_as_of: string;
  data_version: string;
  model_version: string;
  reka_model: string;
  refusal_code: string;
}

export interface ModelCard {
  model_name: string;
  model_version: string;
  data_version: string;
  primary_metric: { name: string; value: number; split: string; definition: string };
  baseline_comparison: {
    baseline_model: string;
    baseline_value: number;
    selected_value: number;
    selected_model_beats_baseline: boolean;
  };
  intended_uses: string[];
  prohibited_uses: string[];
  limitations: string[];
  human_review_required: boolean;
}

export const DEMO_TENANTS = [
  { label: "Demo Tenant One", token: "demo-token-one" },
  { label: "Demo Tenant Two", token: "demo-token-two" },
] as const;

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    throw new Error(`${response.status}: ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  metadata: (token: string) => request<Metadata>("/v1/metadata", token),
  tenants: (token: string) => request<{ tenants: TenantInfo[] }>("/v1/me/tenants", token),
  sources: (token: string) => request<{ sources: SourceInfo[] }>("/v1/sources", token),
  risk: (token: string, windowStart: string, category: string) =>
    request<RiskCollection>(
      `/v1/risk?window_start=${encodeURIComponent(windowStart)}&category=${category}`,
      token,
    ),
  explanation: (token: string, cellId: string, windowStart: string, category: string) =>
    request<CellExplanation>(
      `/v1/cells/${cellId}/explanation?window_start=${encodeURIComponent(windowStart)}&category=${category}`,
      token,
    ),
  modelCard: (token: string) => request<ModelCard>("/v1/model-card", token),
  copilot: (token: string, question: string) =>
    request<CopilotInsight>("/v1/ai/copilot/messages", token, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
};
