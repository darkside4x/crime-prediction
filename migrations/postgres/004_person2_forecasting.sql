BEGIN;

CREATE TABLE IF NOT EXISTS operational_forecasts (
  tenant_id uuid NOT NULL,
  forecast_id uuid NOT NULL,
  cell_id text NOT NULL,
  window_start timestamptz NOT NULL,
  category text NOT NULL,
  feature_snapshot_version text NOT NULL,
  model_version text NOT NULL,
  forecast jsonb NOT NULL,
  generated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, cell_id, window_start, category),
  UNIQUE (tenant_id, forecast_id)
);
CREATE INDEX IF NOT EXISTS operational_forecasts_tenant_window
  ON operational_forecasts (tenant_id, window_start, category);

CREATE TABLE IF NOT EXISTS api_idempotency_records (
  tenant_id uuid NOT NULL,
  operation text NOT NULL,
  idempotency_key text NOT NULL,
  payload_digest text NOT NULL,
  state text NOT NULL CHECK (state IN ('running','completed')),
  owner_token uuid NOT NULL,
  result jsonb,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS api_audit_events (
  tenant_id uuid NOT NULL,
  audit_id uuid NOT NULL,
  principal_id text NOT NULL,
  request_id uuid NOT NULL,
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id text NOT NULL,
  outcome text NOT NULL,
  occurred_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, audit_id)
);

ALTER TABLE operational_forecasts ENABLE ROW LEVEL SECURITY;
ALTER TABLE operational_forecasts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS operational_forecasts_tenant_isolation ON operational_forecasts;
CREATE POLICY operational_forecasts_tenant_isolation ON operational_forecasts
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['api_idempotency_records', 'api_audit_events'] LOOP
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
