-- Tenant-scoped, human-authorized voice-notification persistence.
-- The application must SET LOCAL app.tenant_id for every tenant transaction.
-- Callable destinations remain in Secrets Manager; this database stores only
-- a secret:// reference and a masked projection suitable for reviewer UIs.
BEGIN;

CREATE TABLE IF NOT EXISTS response_contacts (
  tenant_id uuid NOT NULL,
  contact_id uuid NOT NULL,
  zone_id text NOT NULL CHECK (zone_id ~ '^[a-z0-9][a-z0-9_-]{0,79}$'),
  broad_location_label text NOT NULL CHECK (char_length(broad_location_label) BETWEEN 1 AND 120),
  coverage_h3_cells text[] NOT NULL CHECK (cardinality(coverage_h3_cells) BETWEEN 1 AND 256),
  role text NOT NULL CHECK (role IN ('primary', 'supervisor')),
  contact_label text NOT NULL CHECK (char_length(contact_label) BETWEEN 1 AND 120),
  destination_secret_ref text NOT NULL
    CHECK (left(destination_secret_ref, 9) = 'secret://' AND char_length(destination_secret_ref) <= 512),
  masked_destination text NOT NULL
    CHECK (char_length(masked_destination) BETWEEN 4 AND 32 AND position('*' IN masked_destination) > 0),
  timezone text NOT NULL CHECK (char_length(timezone) BETWEEN 1 AND 80),
  calling_days smallint[] NOT NULL DEFAULT ARRAY[0,1,2,3,4,5,6]::smallint[]
    CHECK (
      cardinality(calling_days) BETWEEN 1 AND 7
      AND calling_days <@ ARRAY[0,1,2,3,4,5,6]::smallint[]
    ),
  calling_window_start time NOT NULL,
  calling_window_end time NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  opted_in_at timestamptz,
  verified_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, contact_id)
);

-- Directory resolution requires at most one active contact per role and zone.
-- The service fails closed unless it finds both one primary and one supervisor.
CREATE UNIQUE INDEX IF NOT EXISTS response_contacts_one_enabled_role_per_zone
  ON response_contacts (tenant_id, zone_id, role) WHERE enabled;
CREATE INDEX IF NOT EXISTS response_contacts_zone_lookup
  ON response_contacts (tenant_id, zone_id, enabled);
CREATE INDEX IF NOT EXISTS response_contacts_h3_lookup
  ON response_contacts USING gin (coverage_h3_cells);

CREATE TABLE IF NOT EXISTS dispatch_cases (
  tenant_id uuid NOT NULL,
  dispatch_case_id uuid NOT NULL,
  incident_id uuid NOT NULL,
  review_id uuid NOT NULL,
  incident_source_id uuid NOT NULL,
  incident_external_event_id text NOT NULL CHECK (char_length(incident_external_event_id) BETWEEN 1 AND 256),
  case_reference text NOT NULL CHECK (case_reference ~ '^[A-Z0-9-]{4,32}$'),
  confirmed_category text NOT NULL
    CHECK (confirmed_category IN ('property', 'violence', 'public_order', 'traffic_safety', 'other')),
  occurred_at timestamptz NOT NULL,
  broad_location_label text NOT NULL CHECK (char_length(broad_location_label) BETWEEN 1 AND 120),
  zone_id text NOT NULL CHECK (zone_id ~ '^[a-z0-9][a-z0-9_-]{0,79}$'),
  primary_contact_id uuid NOT NULL,
  supervisor_contact_id uuid NOT NULL,
  call_authorized boolean NOT NULL CHECK (call_authorized),
  authorized_by text NOT NULL CHECK (char_length(authorized_by) BETWEEN 1 AND 200),
  authorized_at timestamptz NOT NULL,
  authorization_fingerprint text NOT NULL
    CHECK (authorization_fingerprint ~ '^[a-f0-9]{64}$'),
  idempotency_key_hash text NOT NULL CHECK (char_length(idempotency_key_hash) = 64),
  policy_version text NOT NULL CHECK (char_length(policy_version) BETWEEN 1 AND 80),
  message_template_version text NOT NULL CHECK (char_length(message_template_version) BETWEEN 1 AND 80),
  maximum_attempts smallint NOT NULL DEFAULT 3 CHECK (maximum_attempts = 3),
  retry_delay_seconds integer NOT NULL CHECK (retry_delay_seconds BETWEEN 5 AND 3600),
  state text NOT NULL DEFAULT 'queued' CHECK (state IN (
    'queued', 'dialing', 'awaiting_acknowledgement', 'provider_retry',
    'retry_scheduled', 'supervisor_scheduled', 'acknowledged',
    'manual_follow_up', 'unacknowledged', 'canceled'
  )),
  attempt_count smallint NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
  next_attempt_at timestamptz,
  final_outcome text CHECK (final_outcome IS NULL OR final_outcome IN (
    'acknowledged', 'manual_follow_up', 'unacknowledged', 'canceled'
  )),
  closed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  PRIMARY KEY (tenant_id, dispatch_case_id),
  UNIQUE (tenant_id, incident_id),
  UNIQUE (tenant_id, review_id),
  UNIQUE (tenant_id, incident_source_id, incident_external_event_id),
  UNIQUE (tenant_id, case_reference),
  UNIQUE (tenant_id, idempotency_key_hash),
  CHECK (primary_contact_id <> supervisor_contact_id),
  CHECK (
    (state IN ('acknowledged', 'manual_follow_up', 'unacknowledged', 'canceled')
      AND final_outcome IS NOT NULL AND closed_at IS NOT NULL AND next_attempt_at IS NULL)
    OR
    (state NOT IN ('acknowledged', 'manual_follow_up', 'unacknowledged', 'canceled')
      AND final_outcome IS NULL AND closed_at IS NULL)
  ),
  FOREIGN KEY (tenant_id, review_id)
    REFERENCES candidate_reviews_restricted (tenant_id, review_id),
  FOREIGN KEY (tenant_id, incident_source_id, incident_external_event_id)
    REFERENCES accepted_incident_events_restricted (tenant_id, source_id, external_event_id),
  FOREIGN KEY (tenant_id, primary_contact_id)
    REFERENCES response_contacts (tenant_id, contact_id),
  FOREIGN KEY (tenant_id, supervisor_contact_id)
    REFERENCES response_contacts (tenant_id, contact_id)
);

CREATE INDEX IF NOT EXISTS dispatch_cases_ready
  ON dispatch_cases (tenant_id, next_attempt_at)
  WHERE state IN ('queued', 'provider_retry', 'retry_scheduled', 'supervisor_scheduled');
CREATE INDEX IF NOT EXISTS dispatch_cases_status
  ON dispatch_cases (tenant_id, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS dispatch_call_attempts (
  tenant_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  dispatch_case_id uuid NOT NULL,
  attempt_number smallint NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
  recipient_role text NOT NULL CHECK (recipient_role IN ('primary', 'supervisor')),
  contact_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'reserved' CHECK (state IN (
    'reserved', 'provider_retry', 'initiated', 'ringing', 'answered',
    'acknowledged', 'manual_follow_up', 'unacknowledged', 'canceled'
  )),
  outcome text CHECK (outcome IS NULL OR outcome IN (
    'acknowledged', 'callback_requested', 'no_answer', 'busy',
    'failed', 'no_acknowledgement', 'canceled'
  )),
  scheduled_at timestamptz NOT NULL,
  initiated_at timestamptz,
  answered_at timestamptz,
  completed_at timestamptz,
  next_action_at timestamptz,
  provider_call_id_hash text CHECK (provider_call_id_hash IS NULL OR char_length(provider_call_id_hash) = 64),
  callback_token_hash text CHECK (callback_token_hash IS NULL OR char_length(callback_token_hash) = 64),
  safe_error_code text CHECK (safe_error_code IS NULL OR safe_error_code ~ '^[a-z0-9_]{1,80}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  PRIMARY KEY (tenant_id, attempt_id),
  UNIQUE (tenant_id, dispatch_case_id, attempt_number),
  UNIQUE (tenant_id, attempt_id, dispatch_case_id, attempt_number, recipient_role),
  CHECK (
    (attempt_number IN (1, 2) AND recipient_role = 'primary')
    OR (attempt_number = 3 AND recipient_role = 'supervisor')
  ),
  FOREIGN KEY (tenant_id, dispatch_case_id)
    REFERENCES dispatch_cases (tenant_id, dispatch_case_id),
  FOREIGN KEY (tenant_id, contact_id)
    REFERENCES response_contacts (tenant_id, contact_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS dispatch_call_attempts_provider_call
  ON dispatch_call_attempts (tenant_id, provider_call_id_hash)
  WHERE provider_call_id_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS dispatch_call_attempts_callback_token
  ON dispatch_call_attempts (tenant_id, callback_token_hash)
  WHERE callback_token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS dispatch_call_attempts_ready
  ON dispatch_call_attempts (tenant_id, scheduled_at)
  WHERE state IN ('reserved', 'provider_retry');

CREATE TABLE IF NOT EXISTS dispatch_events (
  tenant_id uuid NOT NULL,
  event_id uuid NOT NULL,
  dispatch_case_id uuid NOT NULL,
  attempt_id uuid,
  attempt_number smallint CHECK (attempt_number IS NULL OR attempt_number BETWEEN 1 AND 3),
  event_type text NOT NULL CHECK (event_type IN (
    'authorized', 'attempt_reserved', 'call_initiated', 'provider_retry',
    'provider_status', 'answering_machine', 'acknowledged',
    'manual_follow_up', 'invalid_gather_input', 'retry_scheduled',
    'supervisor_scheduled', 'exhausted', 'canceled'
  )),
  actor_type text NOT NULL CHECK (actor_type IN ('reviewer', 'system', 'provider', 'contact')),
  recipient_role text CHECK (recipient_role IS NULL OR recipient_role IN ('primary', 'supervisor')),
  safe_code text CHECK (safe_code IS NULL OR safe_code ~ '^[a-z0-9_]{1,80}$'),
  dedupe_key_hash text NOT NULL CHECK (char_length(dedupe_key_hash) = 64),
  occurred_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, event_id),
  UNIQUE (tenant_id, dedupe_key_hash),
  CHECK (
    (attempt_id IS NULL AND attempt_number IS NULL AND recipient_role IS NULL)
    OR (attempt_id IS NOT NULL AND attempt_number IS NOT NULL AND recipient_role IS NOT NULL)
  ),
  FOREIGN KEY (tenant_id, dispatch_case_id)
    REFERENCES dispatch_cases (tenant_id, dispatch_case_id),
  FOREIGN KEY (tenant_id, attempt_id, dispatch_case_id, attempt_number, recipient_role)
    REFERENCES dispatch_call_attempts (
      tenant_id, attempt_id, dispatch_case_id, attempt_number, recipient_role
    )
);

CREATE INDEX IF NOT EXISTS dispatch_events_case_timeline
  ON dispatch_events (tenant_id, dispatch_case_id, occurred_at, event_id);

-- Deliberately non-RLS: a signed Twilio webhook knows only an opaque path token.
-- The service hashes that token, resolves this minimal route, then opens a
-- normal RLS transaction using the returned tenant. No raw token, phone value,
-- provider call identifier, or webhook payload is stored here.
CREATE TABLE IF NOT EXISTS dispatch_callback_routes (
  callback_token_hash text PRIMARY KEY CHECK (char_length(callback_token_hash) = 64),
  tenant_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS dispatch_callback_routes_expiry
  ON dispatch_callback_routes (expires_at);

CREATE OR REPLACE FUNCTION app.validate_dispatch_case_links()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  review_document jsonb;
  detection_source_id uuid;
  incident_category text;
  contact_role text;
  contact_zone text;
  contact_enabled boolean;
BEGIN
  IF TG_OP = 'UPDATE' AND ROW(
    NEW.tenant_id, NEW.incident_id, NEW.review_id, NEW.incident_source_id,
    NEW.incident_external_event_id, NEW.case_reference, NEW.zone_id,
    NEW.primary_contact_id, NEW.supervisor_contact_id, NEW.call_authorized,
    NEW.authorized_by, NEW.authorized_at, NEW.authorization_fingerprint,
    NEW.idempotency_key_hash,
    NEW.policy_version, NEW.message_template_version, NEW.maximum_attempts,
    NEW.retry_delay_seconds
  ) IS DISTINCT FROM ROW(
    OLD.tenant_id, OLD.incident_id, OLD.review_id, OLD.incident_source_id,
    OLD.incident_external_event_id, OLD.case_reference, OLD.zone_id,
    OLD.primary_contact_id, OLD.supervisor_contact_id, OLD.call_authorized,
    OLD.authorized_by, OLD.authorized_at, OLD.authorization_fingerprint,
    OLD.idempotency_key_hash,
    OLD.policy_version, OLD.message_template_version, OLD.maximum_attempts,
    OLD.retry_delay_seconds
  ) THEN
    RAISE EXCEPTION 'dispatch confirmation and authorization links are immutable'
      USING ERRCODE = '23514';
  END IF;

  IF TG_OP = 'UPDATE' THEN
    RETURN NEW;
  END IF;

  SELECT review.decision, detection.source_id
    INTO review_document, detection_source_id
  FROM candidate_reviews_restricted AS review
  JOIN candidate_detections_restricted AS detection
    ON detection.tenant_id = review.tenant_id
   AND detection.detection_id = review.detection_id
  WHERE review.tenant_id = NEW.tenant_id
    AND review.review_id = NEW.review_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dispatch requires an existing reviewed detection'
      USING ERRCODE = '23503';
  END IF;
  IF review_document ->> 'decision' IS DISTINCT FROM 'confirmed' THEN
    RAISE EXCEPTION 'dispatch requires an immutable confirmed review'
      USING ERRCODE = '23514';
  END IF;
  IF review_document ->> 'promoted_external_event_id'
      IS DISTINCT FROM NEW.incident_external_event_id THEN
    RAISE EXCEPTION 'dispatch incident does not match the confirmed review promotion'
      USING ERRCODE = '23514';
  END IF;
  IF detection_source_id IS DISTINCT FROM NEW.incident_source_id THEN
    RAISE EXCEPTION 'dispatch incident source does not match the reviewed detection'
      USING ERRCODE = '23514';
  END IF;

  SELECT category
    INTO incident_category
  FROM accepted_incident_events_restricted
  WHERE tenant_id = NEW.tenant_id
    AND source_id = NEW.incident_source_id
    AND external_event_id = NEW.incident_external_event_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'dispatch requires an existing promoted incident'
      USING ERRCODE = '23503';
  END IF;
  IF incident_category IS DISTINCT FROM NEW.confirmed_category THEN
    RAISE EXCEPTION 'dispatch category does not match the confirmed incident'
      USING ERRCODE = '23514';
  END IF;

  SELECT role, zone_id, enabled
    INTO contact_role, contact_zone, contact_enabled
  FROM response_contacts
  WHERE tenant_id = NEW.tenant_id AND contact_id = NEW.primary_contact_id;
  IF NOT FOUND OR contact_role <> 'primary' OR contact_zone <> NEW.zone_id OR NOT contact_enabled THEN
    RAISE EXCEPTION 'dispatch requires one enabled primary contact for its zone'
      USING ERRCODE = '23514';
  END IF;

  SELECT role, zone_id, enabled
    INTO contact_role, contact_zone, contact_enabled
  FROM response_contacts
  WHERE tenant_id = NEW.tenant_id AND contact_id = NEW.supervisor_contact_id;
  IF NOT FOUND OR contact_role <> 'supervisor' OR contact_zone <> NEW.zone_id OR NOT contact_enabled THEN
    RAISE EXCEPTION 'dispatch requires one enabled supervisor contact for its zone'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS dispatch_cases_validate_links ON dispatch_cases;
CREATE TRIGGER dispatch_cases_validate_links
  BEFORE INSERT OR UPDATE ON dispatch_cases
  FOR EACH ROW EXECUTE FUNCTION app.validate_dispatch_case_links();

CREATE OR REPLACE FUNCTION app.validate_dispatch_attempt_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  expected_contact_id uuid;
BEGIN
  IF TG_OP = 'UPDATE' AND ROW(
    NEW.tenant_id, NEW.dispatch_case_id, NEW.attempt_number,
    NEW.recipient_role, NEW.contact_id
  ) IS DISTINCT FROM ROW(
    OLD.tenant_id, OLD.dispatch_case_id, OLD.attempt_number,
    OLD.recipient_role, OLD.contact_id
  ) THEN
    RAISE EXCEPTION 'dispatch attempt identity and target are immutable'
      USING ERRCODE = '23514';
  END IF;

  SELECT CASE
    WHEN NEW.attempt_number IN (1, 2) THEN primary_contact_id
    ELSE supervisor_contact_id
  END
  INTO expected_contact_id
  FROM dispatch_cases
  WHERE tenant_id = NEW.tenant_id AND dispatch_case_id = NEW.dispatch_case_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dispatch attempt requires an existing case'
      USING ERRCODE = '23503';
  END IF;
  IF NEW.contact_id IS DISTINCT FROM expected_contact_id THEN
    RAISE EXCEPTION 'dispatch attempt target does not match the escalation policy'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS dispatch_call_attempts_validate_target ON dispatch_call_attempts;
CREATE TRIGGER dispatch_call_attempts_validate_target
  BEFORE INSERT OR UPDATE ON dispatch_call_attempts
  FOR EACH ROW EXECUTE FUNCTION app.validate_dispatch_attempt_target();

CREATE OR REPLACE FUNCTION app.reject_dispatch_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'dispatch events are append-only' USING ERRCODE = '23514';
END;
$$;

DROP TRIGGER IF EXISTS dispatch_events_append_only ON dispatch_events;
CREATE TRIGGER dispatch_events_append_only
  BEFORE UPDATE OR DELETE ON dispatch_events
  FOR EACH ROW EXECUTE FUNCTION app.reject_dispatch_event_mutation();

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'response_contacts', 'dispatch_cases', 'dispatch_call_attempts', 'dispatch_events'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', table_name || '_tenant_isolation', table_name);
    EXECUTE format(
      'CREATE POLICY %I ON %I USING (tenant_id = app.current_tenant_id()) WITH CHECK (tenant_id = app.current_tenant_id())',
      table_name || '_tenant_isolation', table_name
    );
  END LOOP;
END $$;

COMMIT;
