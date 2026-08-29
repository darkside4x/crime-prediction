-- Cross-replica transport rate limiting. Keys are double-hashed bearer/IP
-- digests and contain no tenant payload or raw credential.
BEGIN;

CREATE TABLE IF NOT EXISTS api_rate_limit_buckets (
  key_hash text NOT NULL CHECK (length(key_hash) = 64),
  window_start bigint NOT NULL,
  request_count integer NOT NULL CHECK (request_count > 0),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (key_hash, window_start)
);

CREATE INDEX IF NOT EXISTS api_rate_limit_buckets_expiry
  ON api_rate_limit_buckets (expires_at);

COMMIT;
