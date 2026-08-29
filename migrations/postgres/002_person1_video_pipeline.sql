-- Person 1 production persistence. The application must SET LOCAL
-- app.tenant_id = '<authenticated tenant uuid>' at transaction start.
BEGIN;

CREATE SCHEMA IF NOT EXISTS app;
CREATE OR REPLACE FUNCTION app.current_tenant_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE TABLE IF NOT EXISTS camera_sources (
  tenant_id uuid NOT NULL,
  source_id uuid NOT NULL,
  definition jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source_id)
);

CREATE TABLE IF NOT EXISTS video_assets_restricted (
  tenant_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  source_id uuid NOT NULL,
  metadata jsonb NOT NULL,
  local_storage_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, asset_id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES camera_sources (tenant_id, source_id)
);

CREATE TABLE IF NOT EXISTS reka_video_registry_restricted (
  tenant_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  source_id uuid NOT NULL,
  reka_video_id text NOT NULL,
  indexing_status text NOT NULL,
  remote_deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, asset_id),
  UNIQUE (tenant_id, reka_video_id),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES video_assets_restricted (tenant_id, asset_id)
);

CREATE TABLE IF NOT EXISTS video_processing_jobs (
  tenant_id uuid NOT NULL,
  job_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  operation text NOT NULL CHECK (operation IN ('upload','index','analyze','delete')),
  idempotency_key text NOT NULL,
  state text NOT NULL CHECK (state IN ('queued','running','completed','failed','cancelled','retry')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL CHECK (max_attempts > 0),
  last_error_code text,
  available_at timestamptz NOT NULL DEFAULT now(),
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, job_id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES video_assets_restricted (tenant_id, asset_id)
);
CREATE INDEX IF NOT EXISTS video_processing_jobs_ready
  ON video_processing_jobs (state, available_at) WHERE state IN ('queued','retry');

CREATE TABLE IF NOT EXISTS candidate_detections_restricted (
  tenant_id uuid NOT NULL,
  detection_id uuid NOT NULL,
  source_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  semantic_key text NOT NULL,
  candidate jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, detection_id),
  UNIQUE (tenant_id, semantic_key),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES video_assets_restricted (tenant_id, asset_id)
);

CREATE TABLE IF NOT EXISTS candidate_reviews_restricted (
  tenant_id uuid NOT NULL,
  review_id uuid NOT NULL,
  detection_id uuid NOT NULL,
  decision jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, review_id),
  UNIQUE (tenant_id, detection_id),
  FOREIGN KEY (tenant_id, detection_id) REFERENCES candidate_detections_restricted (tenant_id, detection_id)
);

CREATE TABLE IF NOT EXISTS confirmed_incident_events_restricted (
  tenant_id uuid NOT NULL,
  source_id uuid NOT NULL,
  external_event_id text NOT NULL,
  event jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source_id, external_event_id)
);

CREATE TABLE IF NOT EXISTS coverage_snapshots (
  tenant_id uuid NOT NULL,
  source_id uuid NOT NULL,
  interval_start timestamptz NOT NULL,
  interval_end timestamptz NOT NULL,
  snapshot jsonb NOT NULL,
  PRIMARY KEY (tenant_id, source_id, interval_start)
);

CREATE TABLE IF NOT EXISTS future_feature_snapshots (
  tenant_id uuid NOT NULL,
  snapshot_version text NOT NULL,
  interval_start timestamptz NOT NULL,
  rows jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, snapshot_version)
);

CREATE TABLE IF NOT EXISTS video_audit_log (
  tenant_id uuid NOT NULL,
  audit_id uuid NOT NULL,
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id text NOT NULL,
  outcome text NOT NULL,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, audit_id)
);

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'camera_sources', 'video_assets_restricted', 'reka_video_registry_restricted',
    'video_processing_jobs', 'candidate_detections_restricted',
    'candidate_reviews_restricted', 'confirmed_incident_events_restricted',
    'coverage_snapshots', 'future_feature_snapshots', 'video_audit_log'
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
