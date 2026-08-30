from __future__ import annotations

from pathlib import Path

import pytest

from src.data.video.cli import (
    _assert_migrator_connection,
    _migration_database_url,
)


class FakeCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


def test_migration_url_never_falls_back_to_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://crime_app:runtime@db/app")
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("DATABASE_MIGRATOR_URL", raising=False)
    monkeypatch.delenv("DATABASE_MIGRATOR_URL_FILE", raising=False)

    with pytest.raises(ValueError, match="DATABASE_MIGRATOR_URL is required"):
        _migration_database_url()

    monkeypatch.setenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql://crime_migrator:migrator@db/app",
    )
    assert "crime_migrator" in _migration_database_url()


def test_migration_url_supports_a_protected_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "database-migrator-url"
    secret_file.write_text(
        "postgresql://crime_migrator:migrator@db/app", encoding="utf-8"
    )
    monkeypatch.delenv("DATABASE_MIGRATOR_URL", raising=False)
    monkeypatch.setenv("DATABASE_MIGRATOR_URL_FILE", str(secret_file))

    assert _migration_database_url().startswith("postgresql://crime_migrator:")


def test_migration_connection_accepts_only_the_bounded_schema_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_MIGRATOR_ROLE", raising=False)
    cursor = FakeCursor(
        (
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
    )

    _assert_migrator_connection(cursor)

    assert "rolbypassrls" in cursor.statements[0]
    assert "has_database_privilege" in cursor.statements[0]


@pytest.mark.parametrize(
    "row",
    (
        (
            "crime_app",
            "crime_app",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
        ),
        (
            "crime_migrator",
            "platform_admin",
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
        ),
        (
            "crime_migrator",
            "crime_migrator",
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
        ),
        (
            "crime_migrator",
            "crime_migrator",
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
        ),
        (
            "crime_migrator",
            "crime_migrator",
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
        ),
        (
            "crime_migrator",
            "crime_migrator",
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
        ),
    ),
)
def test_migration_connection_rejects_runtime_or_overprivileged_roles(
    row: tuple[object, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_MIGRATOR_ROLE", raising=False)

    with pytest.raises(RuntimeError):
        _assert_migrator_connection(FakeCursor(row))


def test_migration_role_name_cannot_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_MIGRATOR_ROLE", "platform_admin")

    with pytest.raises(ValueError, match="must be crime_migrator"):
        _assert_migrator_connection(FakeCursor(None))
