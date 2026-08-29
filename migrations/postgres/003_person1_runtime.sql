-- Person 1 runtime persistence and job-leasing hardening.
-- Run as the schema owner, then connect the application with a non-BYPASSRLS role.
BEGIN;

CREATE TABLE IF NOT EXISTS incident_sources_restricted (
  tenant_id uuid NOT NULL,
  source_id uuid NOT NULL,
  definition jsonb NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source_id)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  source_id uuid NOT NULL,
  mode text NOT NULL,
  status text NOT NULL,
  checkpoint jsonb,
  accepted_count integer NOT NULL DEFAULT 0,
  duplicate_count integer NOT NULL DEFAULT 0,
  rejected_count integer NOT NULL DEFAULT 0,
  last_received_at timestamptz,
  last_event_at timestamptz,
  last_error_code text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  PRIMARY KEY (tenant_id, run_id)
);

CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
  tenant_id uuid NOT NULL,
  source_id uuid NOT NULL,
  checkpoint jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source_id)
);

CREATE TABLE IF NOT EXISTS accepted_incident_events_restricted (
  tenant_id uuid NOT NULL,
  source_id uuid NOT NULL,
  external_event_id text NOT NULL,
  event jsonb NOT NULL,
  event_hash text NOT NULL,
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL,
  category text NOT NULL,
  latitude double precision NOT NULL,
  longitude double precision NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source_id, external_event_id)
);
CREATE INDEX IF NOT EXISTS accepted_incident_events_tenant_time
  ON accepted_incident_events_restricted (tenant_id, occurred_at);

CREATE TABLE IF NOT EXISTS ingestion_quarantine_restricted (
  tenant_id uuid NOT NULL,
  quarantine_id uuid NOT NULL,
  source_id uuid NOT NULL,
  checkpoint jsonb,
  reason_code text NOT NULL,
  safe_detail text NOT NULL,
  payload jsonb,
  payload_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, quarantine_id)
);

ALTER TABLE video_processing_jobs
  ADD COLUMN IF NOT EXISTS started_at timestamptz,
  ADD COLUMN IF NOT EXISTS finished_at timestamptz,
  ADD COLUMN IF NOT EXISTS dead_lettered_at timestamptz,
  ADD COLUMN IF NOT EXISTS worker_id text;

CREATE TABLE IF NOT EXISTS source_coverage_telemetry (
  tenant_id uuid NOT NULL,
  source_id uuid NOT NULL,
  observed_at timestamptz NOT NULL,
  sample_seconds integer NOT NULL CHECK (sample_seconds > 0),
  connected boolean NOT NULL,
  frame_processable boolean NOT NULL,
  detector_available boolean NOT NULL,
  capture_failure boolean NOT NULL DEFAULT false,
  frame_gap boolean NOT NULL DEFAULT false,
  reka_available boolean NOT NULL DEFAULT true,
  processing_latency_ms integer CHECK (processing_latency_ms IS NULL OR processing_latency_ms >= 0),
  PRIMARY KEY (tenant_id, source_id, observed_at)
);

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'incident_sources_restricted', 'ingestion_runs', 'ingestion_checkpoints',
    'accepted_incident_events_restricted', 'ingestion_quarantine_restricted',
    'source_coverage_telemetry'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies WHERE schemaname = 'public'
        AND tablename = table_name AND policyname = table_name || '_tenant_isolation'
    ) THEN
      EXECUTE format(
        'CREATE POLICY %I ON %I USING (tenant_id = app.current_tenant_id()) WITH CHECK (tenant_id = app.current_tenant_id())',
        table_name || '_tenant_isolation', table_name
      );
    END IF;
  END LOOP;
END $$;

COMMIT;
