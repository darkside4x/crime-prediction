-- Value-free diagnostics for fail-closed video analysis jobs.
BEGIN;

ALTER TABLE video_processing_jobs
  ADD COLUMN IF NOT EXISTS safe_diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'video_processing_jobs_safe_diagnostics_object'
  ) THEN
    ALTER TABLE video_processing_jobs
      ADD CONSTRAINT video_processing_jobs_safe_diagnostics_object
      CHECK (jsonb_typeof(safe_diagnostics) = 'object') NOT VALID;
  END IF;
END $$;

ALTER TABLE video_processing_jobs
  VALIDATE CONSTRAINT video_processing_jobs_safe_diagnostics_object;

CREATE UNIQUE INDEX IF NOT EXISTS video_jobs_one_active_analysis
  ON video_processing_jobs (tenant_id, asset_id)
  WHERE operation='analyze' AND state IN ('queued','running','retry');

COMMIT;
