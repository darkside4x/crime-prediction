DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crime_app') THEN
    CREATE ROLE crime_app LOGIN PASSWORD 'local-development-only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END $$;

ALTER DATABASE crime_prediction OWNER TO crime_app;
GRANT CONNECT ON DATABASE crime_prediction TO crime_app;
