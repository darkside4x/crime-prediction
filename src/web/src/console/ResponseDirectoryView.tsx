import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  newIdempotencyKey,
  type ContactRole,
  type ResponseContact,
  type ResponseContactPatch,
  type ResponseContactWrite,
  type TestCallResult,
} from "../api/client";
import { useAuth } from "./AuthContext";

interface ContactFormState {
  zoneId: string;
  broadLocationLabel: string;
  coverageH3Cells: string;
  displayName: string;
  phoneNumber: string;
  role: ContactRole;
  enabled: boolean;
  optedIn: boolean;
  timezone: string;
  callingWindowStart: string;
  callingWindowEnd: string;
  lastVerifiedAt: string;
}

const TIMEZONES = ["Asia/Kolkata", "America/New_York", "America/Chicago", "UTC"];

function utcInputNow(): string {
  return new Date().toISOString().slice(0, 16);
}

function emptyForm(): ContactFormState {
  return {
    zoneId: "",
    broadLocationLabel: "",
    coverageH3Cells: "",
    displayName: "",
    phoneNumber: "",
    role: "primary",
    enabled: true,
    optedIn: false,
    timezone: "Asia/Kolkata",
    callingWindowStart: "08:00",
    callingWindowEnd: "22:00",
    lastVerifiedAt: utcInputNow(),
  };
}

function editForm(contact: ResponseContact): ContactFormState {
  return {
    zoneId: contact.zone_id,
    broadLocationLabel: contact.broad_location_label,
    coverageH3Cells: contact.coverage_h3_cells.join(", "),
    displayName: contact.display_name,
    phoneNumber: "",
    role: contact.role,
    enabled: contact.enabled,
    optedIn: contact.opted_in_for_demo,
    timezone: contact.timezone,
    callingWindowStart: contact.calling_window_start,
    callingWindowEnd: contact.calling_window_end,
    lastVerifiedAt: contact.last_verified_at.slice(0, 16),
  };
}

function isoFromUtcInput(value: string): string {
  return new Date(`${value}:00Z`).toISOString();
}

function parseCoverageCells(value: string): string[] {
  return [...new Set(value.split(/[\s,]+/).map((cell) => cell.trim().toLowerCase()).filter(Boolean))];
}

function formatUtc(value: string): string {
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function contactError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return error ? "The directory request failed." : null;
  if (error.code === "response_contact_in_use" || error.code === "contact_in_use") {
    return "This contact is referenced by an active dispatch case and cannot be deleted.";
  }
  if (error.code === "test_call_disabled") {
    return "Demo test calls are disabled by the deployment safety gate.";
  }
  if (error.code === "dispatch_contact_unavailable") {
    return "The zone must have one enabled primary and one enabled supervisor.";
  }
  return `${error.message} (${error.code})`;
}

export default function ResponseDirectoryView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ContactFormState>(() => emptyForm());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [testFor, setTestFor] = useState<string | null>(null);
  const [testAuthorized, setTestAuthorized] = useState(false);
  const [testResult, setTestResult] = useState<TestCallResult | null>(null);
  const actionKeys = useRef(new Map<string, string>());

  const keyFor = (action: string) => {
    const existing = actionKeys.current.get(action);
    if (existing) return existing;
    const created = newIdempotencyKey();
    actionKeys.current.set(action, created);
    return created;
  };

  const finishAction = (action: string) => actionKeys.current.delete(action);

  const contacts = useQuery({
    queryKey: ["response-contacts", tenantId],
    queryFn: () => api.responseContacts(token),
  });

  const createContact = useMutation({
    mutationFn: (body: ResponseContactWrite) =>
      api.createResponseContact(token, body, keyFor("create")),
    onSuccess: () => {
      finishAction("create");
      setForm(emptyForm());
      createContact.reset();
      void queryClient.invalidateQueries({ queryKey: ["response-contacts", tenantId] });
    },
  });

  const updateContact = useMutation({
    mutationFn: ({ contactId, body }: { contactId: string; body: ResponseContactPatch }) =>
      api.updateResponseContact(token, contactId, body, keyFor(`update:${contactId}`)),
    onSuccess: (_result, variables) => {
      finishAction(`update:${variables.contactId}`);
      setEditingId(null);
      setForm(emptyForm());
      updateContact.reset();
      void queryClient.invalidateQueries({ queryKey: ["response-contacts", tenantId] });
    },
  });

  const deleteContact = useMutation({
    mutationFn: (contactId: string) =>
      api.deleteResponseContact(token, contactId, keyFor(`delete:${contactId}`)),
    onSuccess: (_result, contactId) => {
      finishAction(`delete:${contactId}`);
      void queryClient.invalidateQueries({ queryKey: ["response-contacts", tenantId] });
    },
  });

  const testCall = useMutation({
    mutationFn: (contactId: string) =>
      api.createResponseContactTestCall(token, contactId, keyFor(`test:${contactId}`)),
    onSuccess: (result, contactId) => {
      finishAction(`test:${contactId}`);
      setTestResult(result);
      setTestAuthorized(false);
    },
  });

  const change = <Key extends keyof ContactFormState>(
    key: Key,
    value: ContactFormState[Key],
  ) => setForm((current) => ({ ...current, [key]: value }));

  const resetForm = () => {
    setEditingId(null);
    setForm(emptyForm());
    createContact.reset();
    updateContact.reset();
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const shared = {
      broad_location_label: form.broadLocationLabel.trim(),
      coverage_h3_cells: parseCoverageCells(form.coverageH3Cells),
      display_name: form.displayName.trim(),
      role: form.role,
      enabled: form.enabled,
      opted_in_for_demo: form.optedIn,
      timezone: form.timezone,
      calling_window_start: form.callingWindowStart,
      calling_window_end: form.callingWindowEnd,
      last_verified_at: isoFromUtcInput(form.lastVerifiedAt),
    };
    if (editingId) {
      const body: ResponseContactPatch = {
        ...shared,
        ...(form.phoneNumber.trim() ? { phone_number: form.phoneNumber.trim() } : {}),
      };
      updateContact.mutate({ contactId: editingId, body });
      return;
    }
    createContact.mutate({
      ...shared,
      zone_id: form.zoneId.trim(),
      phone_number: form.phoneNumber.trim(),
    });
  };

  const saveError = contactError(createContact.error ?? updateContact.error);
  const actionError = contactError(deleteContact.error ?? testCall.error);

  return (
    <section className="response-directory-view">
      <div className="row spread response-heading">
        <div>
          <h2 className="section-title">
            RESPONSE <span className="accent">DIRECTORY</span>
          </h2>
          <p className="muted">
            Tenant-administered, opted-in notification contacts. Destinations are masked
            after entry and resolved only by the server during a human-authorized dispatch.
          </p>
        </div>
        <span className="chip chip-warn">DEMO CONTACTS ONLY</span>
      </div>

      <div className="response-grid">
        <div className="panel">
          <div className="row spread">
            <h3>Coverage contacts</h3>
            <button
              type="button"
              className="ghost"
              disabled={contacts.isFetching}
              onClick={() => void contacts.refetch()}
            >
              {contacts.isFetching ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          <p className="muted small">
            Each demo zone needs exactly one enabled primary and one enabled supervisor.
          </p>
          {contacts.isLoading && <p className="muted">Loading response contacts…</p>}
          {contacts.error && (
            <p className="error-banner" role="alert">
              {contactError(contacts.error)}
            </p>
          )}
          {contacts.data?.items.length === 0 && (
            <p className="muted">No contacts are configured for this tenant.</p>
          )}
          <ul className="response-contact-list">
            {contacts.data?.items.map((contact) => {
              const testOpen = testFor === contact.contact_id;
              return (
                <li key={contact.contact_id}>
                  <div className="row spread">
                    <div>
                      <strong>{contact.display_name}</strong>
                      <p className="muted small">
                        {contact.role} · {contact.phone_masked} · {contact.broad_location_label}
                      </p>
                    </div>
                    <div className="chips">
                      <span className={`chip ${contact.enabled ? "chip-ok" : ""}`}>
                        {contact.enabled ? "enabled" : "disabled"}
                      </span>
                      <span className={`chip ${contact.opted_in_for_demo ? "chip-ok" : "chip-warn"}`}>
                        {contact.opted_in_for_demo ? "demo opt-in verified" : "not opted in"}
                      </span>
                    </div>
                  </div>
                  <dl className="contact-details">
                    <div>
                      <dt>Coverage</dt>
                      <dd>{contact.coverage_h3_cells.length} aggregate H3 {contact.coverage_h3_cells.length === 1 ? "cell" : "cells"}</dd>
                    </div>
                    <div>
                      <dt>Calling hours</dt>
                      <dd>
                        {contact.calling_window_start}–{contact.calling_window_end} {contact.timezone}
                      </dd>
                    </div>
                    <div>
                      <dt>Last verified</dt>
                      <dd>{formatUtc(contact.last_verified_at)} UTC</dd>
                    </div>
                  </dl>
                  <div className="row contact-actions">
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => {
                        createContact.reset();
                        updateContact.reset();
                        setEditingId(contact.contact_id);
                        setForm(editForm(contact));
                        window.scrollTo({ top: 0, behavior: "auto" });
                      }}
                    >
                      Edit contact
                    </button>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => {
                        setTestFor(testOpen ? null : contact.contact_id);
                        setTestAuthorized(false);
                        setTestResult(null);
                        testCall.reset();
                      }}
                    >
                      {testOpen ? "Close test-call gate" : "Open test-call gate"}
                    </button>
                    <button
                      type="button"
                      className="ghost danger-ghost"
                      disabled={deleteContact.isPending}
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete ${contact.display_name}? Active dispatch references will block deletion.`,
                          )
                        ) {
                          deleteContact.mutate(contact.contact_id);
                        }
                      }}
                    >
                      Delete
                    </button>
                  </div>
                  {testOpen && (
                    <div className="test-call-gate">
                      <strong>Opted-in demo test call</strong>
                      <p className="muted small">
                        This can place a real sandbox call when enabled by the deployment. Never
                        enter or call a public emergency number.
                      </p>
                      <label className="row">
                        <input
                          type="checkbox"
                          checked={testAuthorized}
                          disabled={!contact.enabled || !contact.opted_in_for_demo}
                          onChange={(event) => setTestAuthorized(event.target.checked)}
                        />
                        I confirm this masked destination belongs to an opted-in teammate.
                      </label>
                      <button
                        type="button"
                        disabled={
                          !contact.enabled ||
                          !contact.opted_in_for_demo ||
                          !testAuthorized ||
                          testCall.isPending
                        }
                        onClick={() => testCall.mutate(contact.contact_id)}
                      >
                        {testCall.isPending ? "Requesting test call…" : "Place demo test call"}
                      </button>
                    </div>
                  )}
                  {testResult?.contact_id === contact.contact_id && (
                    <p className="ok-banner" role="status">
                      Test call {testResult.state} for {testResult.contact_name} ({testResult.phone_masked}).
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
          {actionError && <p className="error-banner" role="alert">{actionError}</p>}
        </div>

        <div className="panel response-form-panel">
          <h3>{editingId ? "Edit response contact" : "Add response contact"}</h3>
          <p className="muted small">
            The full number is accepted only here, sent over HTTPS, and never returned by the API.
          </p>
          <form className="stacked-form" onSubmit={submit}>
            <label>
              Coverage zone
              <input
                value={form.zoneId}
                maxLength={120}
                disabled={Boolean(editingId)}
                pattern="[A-Za-z0-9][A-Za-z0-9_-]*"
                placeholder="demo-zone-a"
                onChange={(event) => change("zoneId", event.target.value)}
                required
              />
            </label>
            <label>
              Broad location label
              <input
                value={form.broadLocationLabel}
                maxLength={120}
                placeholder="Demo Zone A"
                onChange={(event) => change("broadLocationLabel", event.target.value)}
                required
              />
            </label>
            <label>
              Coverage H3 cells
              <textarea
                value={form.coverageH3Cells}
                rows={3}
                placeholder="8861892581fffff"
                onChange={(event) => change("coverageH3Cells", event.target.value)}
                required
              />
              <span className="muted small">Comma- or space-separated aggregate cell IDs; raw coordinates are never entered here.</span>
            </label>
            <label>
              Contact label
              <input
                value={form.displayName}
                maxLength={120}
                placeholder="Demo Zone primary"
                onChange={(event) => change("displayName", event.target.value)}
                required
              />
            </label>
            <label>
              {editingId ? "Replace phone number (optional)" : "Phone number (E.164)"}
              <input
                type="tel"
                autoComplete="off"
                value={form.phoneNumber}
                pattern="\+[1-9][0-9]{7,14}"
                placeholder={editingId ? "Leave blank to keep the stored number" : "+15551234567"}
                onChange={(event) => change("phoneNumber", event.target.value)}
                required={!editingId}
              />
            </label>
            <label>
              Escalation role
              <select
                value={form.role}
                onChange={(event) => change("role", event.target.value as ContactRole)}
              >
                <option value="primary">Primary — attempts 1 and 2</option>
                <option value="supervisor">Supervisor — attempt 3</option>
              </select>
            </label>
            <label>
              Timezone
              <select value={form.timezone} onChange={(event) => change("timezone", event.target.value)}>
                {TIMEZONES.map((timezone) => <option key={timezone}>{timezone}</option>)}
              </select>
            </label>
            <div className="two-column-fields">
              <label>
                Calling window starts
                <input
                  type="time"
                  value={form.callingWindowStart}
                  onChange={(event) => change("callingWindowStart", event.target.value)}
                  required
                />
              </label>
              <label>
                Calling window ends
                <input
                  type="time"
                  value={form.callingWindowEnd}
                  onChange={(event) => change("callingWindowEnd", event.target.value)}
                  required
                />
              </label>
            </div>
            <label>
              Last verified at (UTC)
              <input
                type="datetime-local"
                value={form.lastVerifiedAt}
                onChange={(event) => change("lastVerifiedAt", event.target.value)}
                required
              />
            </label>
            <label className="row checkbox-label">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(event) => change("enabled", event.target.checked)}
              />
              Enabled for directory resolution
            </label>
            <label className="row checkbox-label">
              <input
                type="checkbox"
                checked={form.optedIn}
                onChange={(event) => change("optedIn", event.target.checked)}
              />
              Opted in for hackathon demo calls
            </label>
            <p className="muted small">
              Opt-in must be documented by the tenant. Saving this field does not place a call.
            </p>
            <div className="row">
              <button
                type="submit"
                disabled={createContact.isPending || updateContact.isPending}
              >
                {createContact.isPending || updateContact.isPending
                  ? "Saving…"
                  : editingId
                    ? "Save contact changes"
                    : "Add masked contact"}
              </button>
              {editingId && <button type="button" className="ghost" onClick={resetForm}>Cancel edit</button>}
            </div>
            {saveError && <p className="error-banner" role="alert">{saveError}</p>}
            {(createContact.isSuccess || updateContact.isSuccess) && (
              <p className="ok-banner" role="status">Contact saved; only its masked destination is displayed.</p>
            )}
          </form>
        </div>
      </div>
    </section>
  );
}
