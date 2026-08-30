#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?Set AWS_REGION}"
: "${DATABASE_MASTER_SECRET_ARN:?Set DATABASE_MASTER_SECRET_ARN}"
: "${DATABASE_RUNTIME_SECRET_ARN:?Set DATABASE_RUNTIME_SECRET_ARN}"
: "${DATABASE_MIGRATOR_SECRET_ARN:?Set DATABASE_MIGRATOR_SECRET_ARN}"
: "${DATABASE_HOST:?Set DATABASE_HOST}"
: "${DATABASE_NAME:=crime_prediction}"

if [[ ! "$DATABASE_NAME" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]]; then
  echo "DATABASE_NAME is invalid" >&2
  exit 2
fi

read_secret() {
  aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$1" \
    --query SecretString \
    --output text
}

# Accept the generated {username,password} value on first bootstrap and the
# sealed PostgreSQL DSN on later credential rotations. Values travel only via
# stdin and shell variables; they are never arguments or command output.
parse_database_credentials() {
  local expected_username=$1
  python3 -c '
import json
import sys
import urllib.parse

expected = sys.argv[1]
raw = sys.stdin.read().strip()
try:
    value = json.loads(raw)
    username = value["username"]
    password = value["password"]
except (json.JSONDecodeError, KeyError, TypeError):
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SystemExit("database credential secret has an unsupported shape")
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
if username != expected or not password:
    raise SystemExit("database credential secret has the wrong role")
print(username)
print(password)
' "$expected_username"
}

master_json=$(read_secret "$DATABASE_MASTER_SECRET_ARN")
runtime_secret=$(read_secret "$DATABASE_RUNTIME_SECRET_ARN")
migrator_secret=$(read_secret "$DATABASE_MIGRATOR_SECRET_ARN")

master_user=$(printf '%s' "$master_json" | jq -er .username)
master_password=$(printf '%s' "$master_json" | jq -er .password)
mapfile -t runtime_credentials < <(
  printf '%s' "$runtime_secret" | parse_database_credentials crime_app
)
mapfile -t migrator_credentials < <(
  printf '%s' "$migrator_secret" | parse_database_credentials crime_migrator
)
if (( ${#runtime_credentials[@]} != 2 || ${#migrator_credentials[@]} != 2 )); then
  echo "Database credential secret could not be parsed" >&2
  exit 2
fi
runtime_password=${runtime_credentials[1]}
migrator_password=${migrator_credentials[1]}

export PGPASSWORD="$master_password"
export CRIME_RUNTIME_PASSWORD="$runtime_password"
export CRIME_MIGRATOR_PASSWORD="$migrator_password"
connection="host=$DATABASE_HOST port=5432 dbname=$DATABASE_NAME user=$master_user sslmode=require"

psql "$connection" \
  -v ON_ERROR_STOP=1 \
  -v database_name="$DATABASE_NAME" <<'SQL'
\getenv runtime_password CRIME_RUNTIME_PASSWORD
\getenv migrator_password CRIME_MIGRATOR_PASSWORD

SELECT 'CREATE ROLE crime_migrator'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crime_migrator')
\gexec
SELECT 'CREATE ROLE crime_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crime_app')
\gexec

ALTER ROLE crime_migrator WITH LOGIN PASSWORD :'migrator_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
ALTER ROLE crime_app WITH LOGIN PASSWORD :'runtime_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- Remove legacy memberships so neither service role can use SET ROLE to
-- recover administrative or BYPASSRLS privileges through another principal.
DO $$
DECLARE inherited_role text;
DECLARE member_role text;
BEGIN
  FOR inherited_role, member_role IN
    SELECT granted.rolname, member.rolname
    FROM pg_auth_members AS membership
    JOIN pg_roles AS granted ON granted.oid = membership.roleid
    JOIN pg_roles AS member ON member.oid = membership.member
    WHERE member.rolname IN ('crime_app', 'crime_migrator')
  LOOP
    EXECUTE format('REVOKE %I FROM %I', inherited_role, member_role);
  END LOOP;
END $$;

-- The bootstrap administrator temporarily joins both roles solely to transfer
-- objects from the legacy single-role deployment. No membership remains.
GRANT crime_app TO CURRENT_USER;
GRANT crime_migrator TO CURRENT_USER;
REASSIGN OWNED BY crime_app TO crime_migrator;

REVOKE CONNECT, TEMPORARY ON DATABASE :"database_name" FROM PUBLIC;
REVOKE ALL PRIVILEGES ON DATABASE :"database_name" FROM crime_app;
REVOKE ALL PRIVILEGES ON DATABASE :"database_name" FROM crime_migrator;
GRANT CONNECT ON DATABASE :"database_name" TO crime_app;
GRANT CONNECT, CREATE ON DATABASE :"database_name" TO crime_migrator;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION crime_migrator;
ALTER SCHEMA app OWNER TO crime_migrator;
REVOKE ALL PRIVILEGES ON SCHEMA public, app FROM crime_app;
GRANT USAGE ON SCHEMA public, app TO crime_app;
GRANT USAGE, CREATE ON SCHEMA public, app TO crime_migrator;

-- Existing objects are transferred above. These grants cover an upgrade from
-- the legacy deployment; default privileges cover every later migration.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM crime_app;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM crime_app;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA app FROM crime_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crime_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO crime_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO crime_app;

ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crime_app;
ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO crime_app;
ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA app
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE crime_migrator IN SCHEMA app
  GRANT EXECUTE ON FUNCTIONS TO crime_app;

ALTER ROLE crime_app SET statement_timeout = '30s';
ALTER ROLE crime_app SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE crime_app SET search_path = public, app;
ALTER ROLE crime_migrator SET statement_timeout = '5min';
ALTER ROLE crime_migrator SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE crime_migrator SET search_path = public, app;

DO $$
DECLARE runtime_oid oid;
BEGIN
  SELECT oid INTO runtime_oid FROM pg_roles WHERE rolname = 'crime_app';
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE oid = runtime_oid
      AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolinherit OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'crime_app has an unsafe role attribute';
  END IF;
  IF has_database_privilege('crime_app', current_database(), 'CREATE')
     OR has_database_privilege('crime_app', current_database(), 'TEMPORARY')
     OR has_schema_privilege('crime_app', 'public', 'CREATE')
     OR has_schema_privilege('crime_app', 'app', 'CREATE') THEN
    RAISE EXCEPTION 'crime_app retains DDL privileges';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member = runtime_oid) THEN
    RAISE EXCEPTION 'crime_app must not inherit or SET ROLE to another role';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE relation.relowner = runtime_oid
      AND namespace.nspname IN ('public', 'app')
  ) OR EXISTS (
    SELECT 1 FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE procedure.proowner = runtime_oid
      AND namespace.nspname IN ('public', 'app')
  ) THEN
    RAISE EXCEPTION 'crime_app still owns database objects';
  END IF;
END $$;

REVOKE crime_app FROM CURRENT_USER;
REVOKE crime_migrator FROM CURRENT_USER;
SQL

encode_password() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))'
}
encoded_runtime_password=$(printf '%s' "$runtime_password" | encode_password)
encoded_migrator_password=$(printf '%s' "$migrator_password" | encode_password)
runtime_database_url="postgresql://crime_app:$encoded_runtime_password@$DATABASE_HOST:5432/$DATABASE_NAME?sslmode=require"
migrator_database_url="postgresql://crime_migrator:$encoded_migrator_password@$DATABASE_HOST:5432/$DATABASE_NAME?sslmode=require"

seal_secret() {
  local secret_id=$1
  local secret_value=$2
  printf '%s' "$secret_value" | aws secretsmanager put-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$secret_id" \
    --secret-string file:///dev/stdin >/dev/null
}
seal_secret "$DATABASE_RUNTIME_SECRET_ARN" "$runtime_database_url"
seal_secret "$DATABASE_MIGRATOR_SECRET_ARN" "$migrator_database_url"

unset master_json runtime_secret migrator_secret master_password runtime_password
unset migrator_password PGPASSWORD CRIME_RUNTIME_PASSWORD CRIME_MIGRATOR_PASSWORD
unset encoded_runtime_password encoded_migrator_password runtime_database_url
unset migrator_database_url runtime_credentials migrator_credentials
echo "Separate database migrator and RLS-constrained runtime roles are ready."
