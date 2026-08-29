/**
 * Typed client for the frozen Phase 2 API surface.
 *
 * Response/domain types come from generated artifacts only:
 *  - `contracts.gen.ts` — generated from `contracts/schemas/*.schema.json`
 *  - `types.gen.ts`    — generated from `contracts/openapi.json`
 * Do not hand-maintain duplicate domain interfaces here.
 */

import type { components } from "./types.gen";
import type {
  AggregateForecastModelCard,
  CameraSource,
  CandidateReviewDecision,
  OperationalAggregateForecast,
  RestrictedCandidateDetection,
  SourceCoverageSnapshot,
  TypedApiError,
} from "./contracts.gen";

export type RecordedSourceCreate = components["schemas"]["RecordedSourceCreate"];
export type ReviewRequest = components["schemas"]["ReviewRequest"];

export type Role = "viewer" | "reviewer" | "tenant_admin" | "platform_operator";

export interface TenantMembership {
  tenant_id: string;
  slug: string;
  display_name: string;
  role: Role;
}

export interface MeTenants {
  active_tenant_id: string;
  tenants: TenantMembership[];
}

export interface Metadata {
  categories: string[];
  h3_resolution: number;
  forecast_window_minutes: number;
  limitations: string[];
}

export interface ForecastPage {
  items: OperationalAggregateForecast[];
  page: number;
  page_size: number;
  total: number;
}

export interface Readiness {
  status: string;
  reka_chat: string;
  video_service: string;
  forecast_models: string;
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

export type PublicCandidate = Omit<RestrictedCandidateDetection, "evidence_ref"> & {
  evidence_available: boolean;
};

export type PublicSource = Pick<
  CameraSource,
  | "schema_version"
  | "tenant_id"
  | "source_id"
  | "name"
  | "mode"
  | "status"
  | "timezone"
  | "retention_policy_days"
  | "created_at"
  | "updated_at"
>;

/** Typed API error carrying the contract `code` for state-specific UI. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly retryable: boolean;

  constructor(status: number, body: Partial<TypedApiError> | undefined, fallback: string) {
    super(body?.message ?? fallback);
    this.status = status;
    this.code = body?.code ?? "unknown_error";
    this.requestId = body?.request_id;
    this.retryable = Boolean(body?.retryable);
  }
}

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export function newIdempotencyKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `key-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function request<T>(
  path: string,
  token: string,
  init?: RequestInit & { idempotencyKey?: string },
): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (init?.idempotencyKey) headers["Idempotency-Key"] = init.idempotencyKey;
  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let body: Partial<TypedApiError> | undefined;
    try {
      body = (await response.json()) as Partial<TypedApiError>;
    } catch {
      body = undefined;
    }
    throw new ApiError(response.status, body, `Request failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  ready: () => fetch(`${BASE}/ready`).then((r) => r.json() as Promise<Readiness>),
  meTenants: (token: string) => request<MeTenants>("/v1/me/tenants", token),
  switchTenant: (token: string, tenantId: string) =>
    request<{ active_tenant_id: string; role: Role }>(
      `/v1/me/active-tenant/${encodeURIComponent(tenantId)}`,
      token,
      { method: "PUT", idempotencyKey: newIdempotencyKey() },
    ),
  metadata: (token: string) => request<Metadata>("/v1/metadata", token),
  sources: (token: string) => request<{ items: PublicSource[] }>("/v1/sources", token),
  createRecordedSource: (token: string, body: RecordedSourceCreate, idempotencyKey: string) =>
    request<PublicSource>("/v1/sources/recorded-video", token, {
      method: "POST",
      body: JSON.stringify(body),
      idempotencyKey,
    }),
  requestVideoUpload: (token: string, idempotencyKey: string) =>
    request<unknown>("/v1/video-assets/uploads", token, {
      method: "POST",
      idempotencyKey,
    }),
  ingestionRun: (token: string, runId: string) =>
    request<unknown>(`/v1/ingestion/runs/${encodeURIComponent(runId)}`, token),
  coverage: (token: string) =>
    request<{ items: SourceCoverageSnapshot[] }>("/v1/coverage", token),
  candidates: (token: string, limit = 50) =>
    request<{ items: PublicCandidate[] }>(`/v1/candidate-detections?limit=${limit}`, token),
  reviewCandidate: (
    token: string,
    detectionId: string,
    body: ReviewRequest,
    idempotencyKey: string,
  ) =>
    request<CandidateReviewDecision>(
      `/v1/candidate-detections/${encodeURIComponent(detectionId)}/review`,
      token,
      { method: "POST", body: JSON.stringify(body), idempotencyKey },
    ),
  forecasts: (
    token: string,
    params: { windowStart: string; category: string; page?: number; pageSize?: number; bbox?: string },
  ) => {
    const query = new URLSearchParams({
      window_start: params.windowStart,
      category: params.category,
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 100),
    });
    if (params.bbox) query.set("bbox", params.bbox);
    return request<ForecastPage>(`/v1/forecasts?${query.toString()}`, token);
  },
  forecastDetail: (token: string, forecastId: string) =>
    request<OperationalAggregateForecast>(
      `/v1/forecasts/${encodeURIComponent(forecastId)}`,
      token,
    ),
  modelCard: (token: string) => request<AggregateForecastModelCard>("/v1/model-card", token),
  copilot: (token: string, question: string) =>
    request<CopilotInsight>("/v1/ai/copilot/messages", token, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
};

export type { OperationalAggregateForecast, SourceCoverageSnapshot, AggregateForecastModelCard };
