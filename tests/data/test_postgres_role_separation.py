from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

MIGRATOR_DSN = os.getenv("TEST_POSTGRES_MIGRATOR_DSN")
RUNTIME_DSN = os.getenv("TEST_POSTGRES_RUNTIME_DSN")
pytestmark = pytest.mark.skipif(
    not MIGRATOR_DSN or not RUNTIME_DSN,
    reason=(
        "Set TEST_POSTGRES_MIGRATOR_DSN and TEST_POSTGRES_RUNTIME_DSN to run "
        "direct role-separation tests"
    ),
)


def _apply_migrations(psycopg: Any) -> None:
    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(MIGRATOR_DSN, autocommit=True) as connection:
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))


def test_runtime_role_has_dml_but_no_ddl_ownership_or_rls_bypass() -> None:
    psycopg = pytest.importorskip("psycopg")
    _apply_migrations(psycopg)
    with psycopg.connect(RUNTIME_DSN, autocommit=True) as connection:
        role = connection.execute(
            """SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit,
                      rolbypassrls
               FROM pg_roles WHERE rolname = current_user"""
        ).fetchone()
        assert role == ("crime_app", False, False, False, False, False)
        privileges = connection.execute(
            """SELECT has_database_privilege(current_database(), 'CREATE'),
                      has_database_privilege(current_database(), 'TEMPORARY'),
                      has_schema_privilege('public', 'CREATE'),
                      has_schema_privilege('app', 'CREATE'),
                      NOT EXISTS (
                        SELECT 1 FROM pg_auth_members
                        WHERE member = (SELECT oid FROM pg_roles WHERE rolname=current_user)
                      )"""
        ).fetchone()
        assert privileges == (False, False, False, False, True)
        owned_objects = connection.execute(
            """SELECT count(*) FROM (
                 SELECT relation.oid FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                 WHERE relation.relowner = (SELECT oid FROM pg_roles WHERE rolname=current_user)
                   AND namespace.nspname IN ('public', 'app')
                 UNION ALL
                 SELECT procedure.oid FROM pg_proc AS procedure
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = procedure.pronamespace
                 WHERE procedure.proowner = (SELECT oid FROM pg_roles WHERE rolname=current_user)
                   AND namespace.nspname IN ('public', 'app')
               ) AS owned"""
        ).fetchone()[0]
        assert owned_objects == 0
        missing_dml = connection.execute(
            """SELECT count(*) FROM pg_tables
               WHERE schemaname='public'
                 AND NOT (
                   has_table_privilege(
                     quote_ident(schemaname) || '.' || quote_ident(tablename), 'SELECT'
                   )
                   AND has_table_privilege(
                     quote_ident(schemaname) || '.' || quote_ident(tablename), 'INSERT'
                   )
                   AND has_table_privilege(
                     quote_ident(schemaname) || '.' || quote_ident(tablename), 'UPDATE'
                   )
                   AND has_table_privilege(
                     quote_ident(schemaname) || '.' || quote_ident(tablename), 'DELETE'
                   )
                 )"""
        ).fetchone()[0]
        assert missing_dml == 0
        missing_sequence_access = connection.execute(
            """SELECT count(*) FROM pg_sequences
               WHERE schemaname='public'
                 AND NOT (
                   has_sequence_privilege(
                     quote_ident(schemaname) || '.' || quote_ident(sequencename),
                     'USAGE'
                   )
                   AND has_sequence_privilege(
                     quote_ident(schemaname) || '.' || quote_ident(sequencename),
                     'SELECT'
                   )
                 )"""
        ).fetchone()[0]
        assert missing_sequence_access == 0
        assert connection.execute(
            "SELECT has_function_privilege('app.current_tenant_id()', 'EXECUTE')"
        ).fetchone() == (True,)
        queue_rls = connection.execute(
            """SELECT relrowsecurity, relforcerowsecurity
               FROM pg_class
               WHERE oid='public.demo_job_messages'::regclass"""
        ).fetchone()
        assert queue_rls == (True, True)
        queue_functions = connection.execute(
            """SELECT procedure.proname, procedure.prosecdef,
                      array_to_string(procedure.proconfig, ','),
                      has_function_privilege(current_user, procedure.oid, 'EXECUTE'),
                      NOT EXISTS (
                        SELECT 1
                        FROM aclexplode(
                          COALESCE(
                            procedure.proacl,
                            acldefault('f', procedure.proowner)
                          )
                        ) AS privilege
                        WHERE privilege.grantee=0
                          AND privilege.privilege_type='EXECUTE'
                      )
               FROM pg_proc AS procedure
               JOIN pg_namespace AS namespace
                 ON namespace.oid=procedure.pronamespace
               WHERE namespace.nspname='app'
                 AND procedure.proname IN (
                   'claim_demo_job_messages', 'demo_job_queue_depth'
                 )
               ORDER BY procedure.proname"""
        ).fetchall()
        assert queue_functions == [
            (
                "claim_demo_job_messages",
                True,
                "search_path=pg_catalog, public",
                True,
                True,
            ),
            (
                "demo_job_queue_depth",
                True,
                "search_path=pg_catalog, public",
                True,
                True,
            ),
        ]

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE TABLE public.runtime_must_not_create(id int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE TEMPORARY TABLE runtime_temp_forbidden(id int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("ALTER TABLE camera_sources DISABLE ROW LEVEL SECURITY")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """ALTER POLICY camera_sources_tenant_isolation ON camera_sources
                   USING (true) WITH CHECK (true)"""
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """CREATE OR REPLACE FUNCTION app.current_tenant_id()
                   RETURNS uuid LANGUAGE sql STABLE AS 'SELECT NULL::uuid'"""
            )


def test_migrator_is_a_bounded_schema_owner_not_an_admin_role() -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(MIGRATOR_DSN, autocommit=True) as connection:
        role = connection.execute(
            """SELECT rolname, session_user, rolsuper, rolcreatedb, rolcreaterole, rolinherit,
                      rolbypassrls,
                      has_database_privilege(current_database(), 'CREATE'),
                      has_schema_privilege('public', 'CREATE'),
                      NOT EXISTS (
                        SELECT 1 FROM pg_auth_members
                        WHERE member = (SELECT oid FROM pg_roles WHERE rolname=current_user)
                      )
               FROM pg_roles WHERE rolname = current_user"""
        ).fetchone()
        assert role == (
            "crime_migrator",
            "crime_migrator",
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
        )
        unexpected_owner = connection.execute(
            """SELECT count(*) FROM pg_class AS relation
               JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
               WHERE namespace.nspname='public'
                 AND relation.relkind IN ('r','p','v','m','S','f')
                 AND relation.relowner <> (SELECT oid FROM pg_roles WHERE rolname=current_user)"""
        ).fetchone()[0]
        assert unexpected_owner == 0
