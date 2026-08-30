-- Exactly-once external-call submission claim.
--
-- SQS is at-least-once.  A logical call-attempt reservation alone cannot stop
-- two workers from submitting the same attempt to Twilio concurrently.  This
-- second, durable claim surrounds the non-transactional provider side effect.
-- A claim is never stolen: expiry means the submission outcome is uncertain
-- and the application must close the case for manual follow-up.
BEGIN;

ALTER TABLE dispatch_call_attempts
  ADD COLUMN IF NOT EXISTS provider_submission_state text NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS provider_submission_owner_hash text,
  ADD COLUMN IF NOT EXISTS provider_submission_claimed_at timestamptz,
  ADD COLUMN IF NOT EXISTS provider_submission_deadline timestamptz,
  ADD COLUMN IF NOT EXISTS provider_submission_completed_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'dispatch_call_attempts_provider_submission_state_check'
      AND conrelid = 'dispatch_call_attempts'::regclass
  ) THEN
    ALTER TABLE dispatch_call_attempts
      ADD CONSTRAINT dispatch_call_attempts_provider_submission_state_check
      CHECK (provider_submission_state IN ('pending', 'claimed', 'submitted', 'uncertain'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'dispatch_call_attempts_provider_submission_shape_check'
      AND conrelid = 'dispatch_call_attempts'::regclass
  ) THEN
    ALTER TABLE dispatch_call_attempts
      ADD CONSTRAINT dispatch_call_attempts_provider_submission_shape_check
      CHECK (
        (
          provider_submission_state = 'pending'
          AND provider_submission_owner_hash IS NULL
          AND provider_submission_claimed_at IS NULL
          AND provider_submission_deadline IS NULL
          AND provider_submission_completed_at IS NULL
        )
        OR
        (
          provider_submission_state = 'claimed'
          AND provider_submission_owner_hash ~ '^[a-f0-9]{64}$'
          AND provider_submission_claimed_at IS NOT NULL
          AND provider_submission_deadline > provider_submission_claimed_at
          AND provider_submission_completed_at IS NULL
        )
        OR
        (
          provider_submission_state = 'submitted'
          AND provider_submission_owner_hash ~ '^[a-f0-9]{64}$'
          AND provider_submission_claimed_at IS NOT NULL
          AND provider_submission_deadline > provider_submission_claimed_at
          AND provider_submission_completed_at >= provider_submission_claimed_at
          AND provider_call_id_hash IS NOT NULL
        )
        OR
        (
          provider_submission_state = 'uncertain'
          AND provider_submission_owner_hash ~ '^[a-f0-9]{64}$'
          AND provider_submission_claimed_at IS NOT NULL
          AND provider_submission_deadline > provider_submission_claimed_at
          AND provider_submission_completed_at >= provider_submission_claimed_at
        )
      );
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS dispatch_call_attempts_submission_claims
  ON dispatch_call_attempts (provider_submission_state, provider_submission_deadline)
  WHERE provider_submission_state = 'claimed';

COMMIT;
