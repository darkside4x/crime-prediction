#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?Set AWS_REGION}"
: "${DATABASE_MASTER_SECRET_ARN:?Set DATABASE_MASTER_SECRET_ARN}"
: "${DATABASE_APP_SECRET_ARN:?Set DATABASE_APP_SECRET_ARN}"
: "${DATABASE_HOST:?Set DATABASE_HOST}"
: "${DATABASE_NAME:=crime_prediction}"

master_json=$(aws secretsmanager get-secret-value \
  --region "$AWS_REGION" \
  --secret-id "$DATABASE_MASTER_SECRET_ARN" \
  --query SecretString \
  --output text)
app_json=$(aws secretsmanager get-secret-value \
  --region "$AWS_REGION" \
  --secret-id "$DATABASE_APP_SECRET_ARN" \
  --query SecretString \
  --output text)
master_user=$(printf '%s' "$master_json" | jq -r .username)
master_password=$(printf '%s' "$master_json" | jq -r .password)
app_user=$(printf '%s' "$app_json" | jq -r .username)
app_password=$(printf '%s' "$app_json" | jq -r .password)
test "$app_user" = crime_app

export PGPASSWORD="$master_password"
connection="host=$DATABASE_HOST port=5432 dbname=$DATABASE_NAME user=$master_user sslmode=require"
if psql "$connection" -tAc "SELECT 1 FROM pg_roles WHERE rolname='crime_app'" | grep -q 1; then
  operation=ALTER
else
  operation=CREATE
fi
psql "$connection" -v ON_ERROR_STOP=1 -c \
  "$operation ROLE crime_app WITH LOGIN PASSWORD '$app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
psql "$connection" -v ON_ERROR_STOP=1 -c \
  "GRANT CONNECT ON DATABASE $DATABASE_NAME TO crime_app"
psql "$connection" -v ON_ERROR_STOP=1 -c \
  "ALTER ROLE crime_app SET statement_timeout = '30s'"
psql "$connection" -v ON_ERROR_STOP=1 -c \
  "ALTER ROLE crime_app SET idle_in_transaction_session_timeout = '30s'"

encoded_password=$(printf '%s' "$app_password" | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')
database_url="postgresql://crime_app:$encoded_password@$DATABASE_HOST:5432/$DATABASE_NAME?sslmode=require"
aws secretsmanager put-secret-value \
  --region "$AWS_REGION" \
  --secret-id "$DATABASE_APP_SECRET_ARN" \
  --secret-string "$database_url" >/dev/null

unset master_json app_json master_password app_password PGPASSWORD encoded_password database_url
echo "Restricted database role created and DSN secret sealed."
