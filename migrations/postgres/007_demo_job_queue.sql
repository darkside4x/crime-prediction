-- Durable broker for the self-contained deployment demo. Production uses SQS.
-- Rows contain opaque routing identifiers only; tenant data remains behind RLS.
BEGIN;

CREATE TABLE IF NOT EXISTS demo_job_messages (
  tenant_id uuid NOT NULL,
  job_id uuid NOT NULL,
  operation text NOT NULL CHECK (operation IN ('upload','index','analyze','delete')),
  state text NOT NULL CHECK (state IN ('queued','leased','dead_letter')) DEFAULT 'queued',
  available_at timestamptz NOT NULL DEFAULT now(),
  lease_expires_at timestamptz,
  receipt uuid,
  receive_count integer NOT NULL DEFAULT 0 CHECK (receive_count >= 0),
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, job_id),
  FOREIGN KEY (tenant_id, job_id) REFERENCES video_processing_jobs (tenant_id, job_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS demo_job_messages_ready
  ON demo_job_messages (operation, available_at)
  WHERE state = 'queued';

COMMIT;
