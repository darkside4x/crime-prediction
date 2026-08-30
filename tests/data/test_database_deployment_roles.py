from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_bootstrap_removes_runtime_ddl_ownership_and_bypass_paths() -> None:
    script = _text("deploy/aws-vm/bootstrap-database.sh")

    for required in (
        "ALTER ROLE crime_app WITH LOGIN",
        "NOINHERIT NOBYPASSRLS",
        "REASSIGN OWNED BY crime_app TO crime_migrator",
        "REVOKE CONNECT, TEMPORARY ON DATABASE",
        "REVOKE ALL PRIVILEGES ON DATABASE",
        "REVOKE ALL PRIVILEGES ON SCHEMA public, app FROM crime_app",
        "GRANT USAGE ON SCHEMA public, app TO crime_app",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES",
        "GRANT USAGE, SELECT ON ALL SEQUENCES",
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO crime_app",
        "has_database_privilege('crime_app', current_database(), 'CREATE')",
        "has_database_privilege('crime_app', current_database(), 'TEMPORARY')",
        "WHERE member.rolname IN ('crime_app', 'crime_migrator')",
        "pg_auth_members WHERE member = runtime_oid",
        "relation.relowner = runtime_oid",
        "procedure.proowner = runtime_oid",
    ):
        assert required in script
    assert "set -x" not in script
    assert 'echo "$runtime_password"' not in script
    assert 'echo "$migrator_password"' not in script
    assert '--secret-string "$' not in script
    assert "--secret-string file:///dev/stdin" in script


def test_production_compose_mounts_only_the_role_each_process_needs() -> None:
    compose = _text("deploy/aws-vm/compose.yml")
    migrate = compose.split("  migrate:\n", 1)[1].split("\n  api:\n", 1)[0]

    assert "DATABASE_MIGRATOR_URL_FILE: /run/secrets/database_migrator_url" in migrate
    assert "- database_migrator_url" in migrate
    assert "database_runtime_url" not in migrate
    assert "REKA_API_KEY" not in migrate
    assert "DATABASE_URL_FILE: /run/secrets/database_runtime_url" in compose
    assert compose.count("- database_migrator_url") == 1
    assert compose.count("- database_runtime_url") >= 3


def test_foundation_limits_migrator_secret_to_the_bootstrap_condition() -> None:
    template = _text("deploy/aws-vm/review2-foundation.yml")
    runtime_secrets = template.split("- Sid: RuntimeSecrets", 1)[1].split(
        "- Sid: DispatchContactSecrets", 1
    )[0]
    bootstrap = template.split("- Sid: OneTimeDatabaseBootstrap", 1)[1].split(
        "- !Ref AWS::NoValue", 1
    )[0]

    assert "MigratorDatabaseSecret:" in template
    assert "crime_migrator" in template
    assert "MigratorDatabaseSecret" not in runtime_secrets
    assert "MigratorDatabaseSecret" in bootstrap
    assert "MigratorDatabaseSecretArn:" in template


def test_foundation_limits_dispatch_contact_secret_mutations() -> None:
    template = _text("deploy/aws-vm/review2-foundation.yml")
    contact_policy = template.split("- Sid: DispatchContactSecrets", 1)[1].split(
        "- !If", 1
    )[0]

    assert "/tenants/*/dispatch-contacts/*" in contact_policy
    assert "/tenants/*\"" not in contact_policy


def test_local_compose_also_separates_migrations_from_runtime() -> None:
    compose = _text("docker-compose.yml")
    init = _text("docker/postgres-init/001-app-role.sql")
    migrate = compose.split("  migrate:\n", 1)[1].split("\n  api:\n", 1)[0]

    assert "DATABASE_MIGRATOR_URL:" in migrate
    assert "crime_migrator" in migrate
    assert "DATABASE_URL:" not in migrate
    assert "ALTER DATABASE crime_prediction OWNER TO crime_app" not in init
    assert "CREATE ROLE crime_migrator" in init
    assert "CREATE ROLE crime_app" in init
