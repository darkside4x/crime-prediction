-- Development-only credentials. Production roles are created from generated
-- Secrets Manager values by deploy/aws-vm/bootstrap-database.sh.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crime_migrator') THEN
    CREATE ROLE crime_migrator LOGIN PASSWORD 'local-migrator-development-only'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crime_app') THEN
    CREATE ROLE crime_app LOGIN PASSWORD 'local-runtime-development-only'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END $$;

REVOKE CONNECT, TEMPORARY ON DATABASE crime_prediction FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION crime_migrator;
GRANT CONNECT ON DATABASE crime_prediction TO crime_app;
GRANT CONNECT, CREATE ON DATABASE crime_prediction TO crime_migrator;
GRANT USAGE ON SCHEMA public, app TO crime_app;
GRANT USAGE, CREATE ON SCHEMA public, app TO crime_migrator;

ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crime_app;
ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO crime_app;
ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA app
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA app
  GRANT EXECUTE ON FUNCTIONS TO crime_app;
