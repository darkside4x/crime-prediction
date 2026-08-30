import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  newIdempotencyKey,
  type DispatchAttempt,
  type DispatchCase,
} from "../api/client";

interface Props {
  token: string;
  tenantId: string;
  initialCase: DispatchCase;
}

const TERMINAL_STATES = new Set([
  "acknowledged",
  "manual_follow_up",
  "unacknowledged",
  "failed",
  "canceled",
]);

function formatUtc(value: string | null): string {
  if (!value) return "Not scheduled";
  return `${new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(value))} UTC`;
}

function stateLabel(value: string): string {
  return value.replace(/_/g, " ");
}

function safeErrorLabel(value: string): string {
  const labels: Record<string, string> = {
    dispatch_delivery_exhausted:
      "Queue delivery retries were exhausted. Manual follow-up is required.",
    voice_submission_uncertain:
      "The provider result was uncertain. No automatic redial will occur.",
    voice_submission_claim_expired:
      "The call submission lease expired. No automatic redial will occur.",
    voice_provider_unavailable:
      "The voice provider was unavailable.",
  };
  return labels[value] ?? `Manual review required (${stateLabel(value)}).`;
}

function AttemptStep({
  number,
  label,
  masked,
  attempt,
}: {
  number: 1 | 2 | 3;
  label: string;
  masked: string;
  attempt?: DispatchAttempt;
}) {
  return (
    <li className={attempt ? `attempt-${attempt.state}` : "attempt-planned"}>
      <span className="timeline-marker" aria-hidden="true">{number}</span>
      <div>
        <div className="row">
          <strong>{number === 3 ? "Supervisor escalation" : `Primary attempt ${number}`}</strong>
          <span className={`chip ${attempt?.state === "acknowledged" ? "chip-ok" : ""}`}>
            {attempt ? stateLabel(attempt.state) : "planned"}
          </span>
        </div>
        <p className="muted small">{attempt?.contact_name ?? label} · {attempt?.phone_masked ?? masked}</p>
        {attempt && <p className="muted small">Updated {formatUtc(attempt.updated_at)}</p>}
        {attempt?.safe_error_code && (
          <p className="error-text small" role="alert">
            {safeErrorLabel(attempt.safe_error_code)}
          </p>
        )}
      </div>
    </li>
  );
}

export default function DispatchCasePanel({ token, tenantId, initialCase }: Props) {
  const queryClient = useQueryClient();
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelReason, setCancelReason] = useState(
    "Canceled by the human reviewer before the next call attempt.",
  );
  const cancelKey = useRef(newIdempotencyKey());

  const caseQuery = useQuery({
    queryKey: ["dispatch-case", tenantId, initialCase.dispatch_case_id],
    queryFn: () => api.dispatchCase(token, initialCase.dispatch_case_id),
    initialData: initialCase,
    refetchInterval: (query) =>
      TERMINAL_STATES.has(query.state.data?.state ?? "") ? false : 3_000,
  });

  const cancel = useMutation({
    mutationFn: () =>
      api.cancelDispatch(
        token,
        initialCase.dispatch_case_id,
        cancelReason.trim(),
        cancelKey.current,
      ),
    onSuccess: (result) => {
      cancelKey.current = newIdempotencyKey();
      setCancelOpen(false);
      queryClient.setQueryData(
        ["dispatch-case", tenantId, initialCase.dispatch_case_id],
        result,
      );
    },
  });

  const dispatchCase = caseQuery.data;
  const attempts = new Map(dispatchCase.attempts.map((attempt) => [attempt.attempt_number, attempt]));
  const cancelable = !TERMINAL_STATES.has(dispatchCase.state);
  const queryError = caseQuery.error instanceof ApiError ? caseQuery.error : null;
  const cancelError = cancel.error instanceof ApiError ? cancel.error : null;

  return (
    <section className="dispatch-case-panel" aria-labelledby={`dispatch-${dispatchCase.dispatch_case_id}`}>
      <div className="row spread">
        <div>
          <p className="eyebrow">Human-authorized notification</p>
          <h3 id={`dispatch-${dispatchCase.dispatch_case_id}`}>Case {dispatchCase.case_reference}</h3>
        </div>
        <span className={`chip dispatch-state state-${dispatchCase.state}`} role="status">
          {stateLabel(dispatchCase.state)}
        </span>
      </div>

      <div className="dispatch-summary-grid">
        <dl className="provenance">
          <dt>Confirmed category</dt>
          <dd>{stateLabel(dispatchCase.category)}</dd>
          <dt>Registered area</dt>
          <dd>{dispatchCase.zone_label}</dd>
          <dt>Occurred at</dt>
          <dd>{formatUtc(dispatchCase.occurred_at)}</dd>
        </dl>
        <dl className="provenance">
          <dt>Authorized by</dt>
          <dd>{dispatchCase.authorized_by_principal_id}</dd>
          <dt>Authorized at</dt>
          <dd>{formatUtc(dispatchCase.authorized_at)}</dd>
          <dt>Next attempt</dt>
          <dd>{formatUtc(dispatchCase.next_attempt_at)}</dd>
        </dl>
      </div>

      <div className="dispatch-policy-note" role="note">
        <strong>Bounded escalation:</strong> at most two calls to the primary contact, then
        one call to the supervisor. Acknowledgement or callback request stops later calls.
      </div>

      <ol className="dispatch-timeline" aria-label="Dispatch attempt timeline">
        <AttemptStep
          number={1}
          label={dispatchCase.primary_contact.display_name}
          masked={dispatchCase.primary_contact.phone_masked}
          attempt={attempts.get(1)}
        />
        <AttemptStep
          number={2}
          label={dispatchCase.primary_contact.display_name}
          masked={dispatchCase.primary_contact.phone_masked}
          attempt={attempts.get(2)}
        />
        <AttemptStep
          number={3}
          label={dispatchCase.supervisor_contact.display_name}
          masked={dispatchCase.supervisor_contact.phone_masked}
          attempt={attempts.get(3)}
        />
      </ol>

      <div className="row">
        <button
          type="button"
          className="ghost"
          disabled={caseQuery.isFetching}
          onClick={() => void caseQuery.refetch()}
        >
          {caseQuery.isFetching ? "Refreshing…" : "Refresh call status"}
        </button>
        {cancelable && !cancelOpen && (
          <button type="button" className="ghost danger-ghost" onClick={() => setCancelOpen(true)}>
            Cancel before next attempt
          </button>
        )}
      </div>

      {cancelOpen && (
        <div className="cancel-dispatch-form">
          <label>
            Cancellation audit reason
            <textarea
              value={cancelReason}
              maxLength={500}
              rows={3}
              onChange={(event) => setCancelReason(event.target.value)}
            />
          </label>
          <p className="muted small">
            This stops pending calls. It does not remove the confirmed incident or call audit.
          </p>
          <div className="row">
            <button
              type="button"
              disabled={cancelReason.trim().length === 0 || cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              {cancel.isPending ? "Canceling pending calls…" : "Confirm cancellation"}
            </button>
            <button type="button" className="ghost" onClick={() => setCancelOpen(false)}>
              Keep escalation active
            </button>
          </div>
        </div>
      )}

      {queryError && (
        <p className="error-banner" role="alert">
          Call status could not be refreshed ({queryError.code}). The durable backend state is unchanged.
        </p>
      )}
      {cancelError && (
        <p className="error-banner" role="alert">
          Pending calls could not be canceled ({cancelError.code}): {cancelError.message}
        </p>
      )}
    </section>
  );
}
