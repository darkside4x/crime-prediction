-- Close the demo broker's tenant-isolation gap. Production continues to use
-- SQS; these functions are only the narrow cross-tenant operations required
-- by the self-contained Postgres demo workers.
BEGIN;

ALTER TABLE public.demo_job_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.demo_job_messages FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS demo_job_messages_tenant_isolation
  ON public.demo_job_messages;
CREATE POLICY demo_job_messages_tenant_isolation
  ON public.demo_job_messages
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

-- FORCE RLS also constrains the table owner. This policy is limited to the
-- bounded schema-owner role used by the SECURITY DEFINER functions below.
-- The runtime role cannot inherit or SET ROLE to crime_migrator.
DROP POLICY IF EXISTS demo_job_messages_migrator_operations
  ON public.demo_job_messages;
CREATE POLICY demo_job_messages_migrator_operations
  ON public.demo_job_messages
  TO crime_migrator
  USING (true)
  WITH CHECK (true);

CREATE OR REPLACE FUNCTION app.claim_demo_job_messages(
  p_operations text[],
  p_limit integer,
  p_visibility_seconds integer
)
RETURNS TABLE (
  tenant_id uuid,
  job_id uuid,
  operation text,
  receipt uuid,
  receive_count integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF p_operations IS NULL
     OR cardinality(p_operations) = 0
     OR EXISTS (
       SELECT 1
       FROM unnest(p_operations) AS requested(value)
       WHERE requested.value IS NULL
          OR requested.value NOT IN ('upload', 'index', 'analyze', 'delete')
     ) THEN
    RAISE EXCEPTION 'Worker operation set is invalid' USING ERRCODE = '22023';
  END IF;
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 10 THEN
    RAISE EXCEPTION 'Worker claim limit is invalid' USING ERRCODE = '22023';
  END IF;
  IF p_visibility_seconds IS NULL
     OR p_visibility_seconds < 1
     OR p_visibility_seconds > 43200 THEN
    RAISE EXCEPTION 'Worker visibility timeout is invalid' USING ERRCODE = '22023';
  END IF;

  RETURN QUERY
  WITH selected AS (
    SELECT message.tenant_id, message.job_id
    FROM public.demo_job_messages AS message
    WHERE message.operation = ANY(p_operations)
      AND message.state <> 'dead_letter'
      AND message.available_at <= statement_timestamp()
      AND (
        message.state = 'queued'
        OR message.lease_expires_at < statement_timestamp()
      )
    ORDER BY message.available_at, message.created_at
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  )
  UPDATE public.demo_job_messages AS message
  SET state = 'leased',
      lease_expires_at = statement_timestamp()
        + make_interval(secs => p_visibility_seconds),
      receipt = gen_random_uuid(),
      receive_count = message.receive_count + 1,
      updated_at = statement_timestamp()
  FROM selected
  WHERE message.tenant_id = selected.tenant_id
    AND message.job_id = selected.job_id
  RETURNING
    message.tenant_id,
    message.job_id,
    message.operation,
    message.receipt,
    message.receive_count;
END;
$$;

CREATE OR REPLACE FUNCTION app.demo_job_queue_depth()
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT count(*)::bigint
  FROM public.demo_job_messages
  WHERE state <> 'dead_letter'
$$;

REVOKE ALL ON FUNCTION app.claim_demo_job_messages(text[], integer, integer)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION app.demo_job_queue_depth()
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.claim_demo_job_messages(text[], integer, integer)
  TO crime_app;
GRANT EXECUTE ON FUNCTION app.demo_job_queue_depth()
  TO crime_app;

COMMIT;
