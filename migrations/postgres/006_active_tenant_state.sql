-- Server-side OIDC tenant selection shared by every API replica. The verified
-- subject is SHA-256 hashed by the application; no bearer token or subject PII
-- enters this operational table.
BEGIN;

CREATE TABLE IF NOT EXISTS principal_active_tenants (
  principal_hash text PRIMARY KEY CHECK (length(principal_hash) = 64),
  tenant_id uuid NOT NULL,
  updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS principal_active_tenants_updated
  ON principal_active_tenants (updated_at);

COMMIT;
