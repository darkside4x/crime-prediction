from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from src.data.dispatch import (
    CallingWindow,
    ContactRole,
    CoverageTarget,
    DispatchContactUnavailable,
    DispatchResourceNotFound,
    InMemoryContactDirectory,
    ResponseContact,
)

TENANT_A = "00000000-0000-4000-8000-000000000001"
TENANT_B = "00000000-0000-4000-8000-000000000002"
CELL = "8860145b49fffff"


def contact(
    contact_id: str,
    role: ContactRole,
    *,
    tenant_id: str = TENANT_A,
    enabled: bool = True,
    cells: frozenset[str] = frozenset({CELL}),
    windows: tuple[CallingWindow, ...] = (),
) -> ResponseContact:
    return ResponseContact(
        contact_id=contact_id,
        tenant_id=tenant_id,
        zone_id="demo-zone-a",
        role=role,
        phone_secret_ref=f"secret://dispatch/{tenant_id}/{contact_id}",
        display_name=f"Demo {role.value}",
        enabled=enabled,
        opted_in_at=datetime(2026, 8, 1, tzinfo=UTC),
        verified_at=datetime(2026, 8, 1, tzinfo=UTC),
        coverage_cells=cells,
        calling_windows=windows,
    )


def test_directory_resolves_exactly_one_contact_per_role_and_tenant() -> None:
    directory = InMemoryContactDirectory(
        [
            contact("primary-a", ContactRole.PRIMARY),
            contact("supervisor-a", ContactRole.SUPERVISOR),
            contact("primary-b", ContactRole.PRIMARY, tenant_id=TENANT_B),
            contact("supervisor-b", ContactRole.SUPERVISOR, tenant_id=TENANT_B),
        ]
    )

    resolved = directory.resolve(
        TENANT_A,
        CoverageTarget(zone_id="demo-zone-a", cell_id=CELL),
        at=datetime(2026, 8, 30, 12, tzinfo=UTC),
    )

    assert resolved.primary.contact_id == "primary-a"
    assert resolved.supervisor.contact_id == "supervisor-a"
    with pytest.raises(DispatchResourceNotFound):
        directory.get_contact(TENANT_B, "primary-a")


def test_directory_fails_closed_for_missing_disabled_or_ambiguous_contacts() -> None:
    target = CoverageTarget(zone_id="demo-zone-a", cell_id=CELL)
    at = datetime(2026, 8, 30, 12, tzinfo=UTC)
    disabled = InMemoryContactDirectory(
        [
            contact("primary-a", ContactRole.PRIMARY, enabled=False),
            contact("supervisor-a", ContactRole.SUPERVISOR),
        ]
    )
    with pytest.raises(DispatchContactUnavailable) as unavailable:
        disabled.resolve(TENANT_A, target, at=at)
    assert unavailable.value.code == "dispatch_contact_unavailable"

    ambiguous = InMemoryContactDirectory(
        [
            contact("primary-a", ContactRole.PRIMARY),
            contact("primary-a2", ContactRole.PRIMARY),
            contact("supervisor-a", ContactRole.SUPERVISOR),
        ]
    )
    with pytest.raises(DispatchContactUnavailable) as duplicate:
        ambiguous.resolve(TENANT_A, target, at=at)
    assert duplicate.value.code == "dispatch_directory_ambiguous"


def test_directory_never_resolves_a_contact_without_recorded_opt_in() -> None:
    primary = contact("primary-a", ContactRole.PRIMARY)
    directory = InMemoryContactDirectory(
        [
            ResponseContact(
                contact_id=primary.contact_id,
                tenant_id=primary.tenant_id,
                zone_id=primary.zone_id,
                role=primary.role,
                phone_secret_ref=primary.phone_secret_ref,
                display_name=primary.display_name,
                coverage_cells=primary.coverage_cells,
            ),
            contact("supervisor-a", ContactRole.SUPERVISOR),
        ]
    )
    with pytest.raises(DispatchContactUnavailable):
        directory.resolve(
            TENANT_A,
            CoverageTarget(zone_id="demo-zone-a", cell_id=CELL),
            at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        )


def test_directory_accepts_aws_secrets_manager_opaque_phone_references() -> None:
    value = ResponseContact(
        contact_id="primary-a",
        tenant_id=TENANT_A,
        zone_id="demo-zone-a",
        role=ContactRole.PRIMARY,
        phone_secret_ref=(
            "secret://aws-secrets-manager/civichalo/tenant-a/dispatch-contact"
        ),
        display_name="Demo primary",
        opted_in_at=datetime(2026, 8, 1, tzinfo=UTC),
        verified_at=datetime(2026, 8, 1, tzinfo=UTC),
        coverage_cells=frozenset({CELL}),
    )
    assert value.phone_secret_ref.startswith("secret://aws-secrets-manager/")


def test_directory_respects_local_calling_windows_including_overnight() -> None:
    # Monday 22:00 through Tuesday 02:00 UTC.
    overnight = CallingWindow(weekdays=frozenset({0}), start=time(22), end=time(2))
    directory = InMemoryContactDirectory(
        [
            contact("primary-a", ContactRole.PRIMARY, windows=(overnight,)),
            contact("supervisor-a", ContactRole.SUPERVISOR, windows=(overnight,)),
        ]
    )
    target = CoverageTarget(zone_id="demo-zone-a", cell_id=CELL)

    assert directory.resolve(TENANT_A, target, at=datetime(2026, 8, 31, 23, tzinfo=UTC))
    assert directory.resolve(TENANT_A, target, at=datetime(2026, 9, 1, 1, tzinfo=UTC))
    with pytest.raises(DispatchContactUnavailable):
        directory.resolve(TENANT_A, target, at=datetime(2026, 9, 1, 3, tzinfo=UTC))
