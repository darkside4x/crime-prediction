"""Tenant-scoped response-directory resolution."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from threading import RLock
from typing import Protocol

from .errors import DispatchContactUnavailable, DispatchResourceNotFound
from .models import (
    ContactRole,
    CoverageTarget,
    ResolvedContacts,
    ResponseContact,
    require_utc,
)


class ContactDirectory(Protocol):
    """Directory interface implemented by memory and future Postgres adapters."""

    def resolve(
        self,
        tenant_id: str,
        coverage: CoverageTarget,
        *,
        at: datetime,
    ) -> ResolvedContacts: ...

    def get_contact(self, tenant_id: str, contact_id: str) -> ResponseContact: ...


class InMemoryContactDirectory:
    """Thread-safe deterministic directory for development and offline tests."""

    def __init__(self, contacts: Iterable[ResponseContact] = ()) -> None:
        self._contacts: dict[tuple[str, str], ResponseContact] = {}
        self._lock = RLock()
        for contact in contacts:
            self.upsert(contact)

    def upsert(self, contact: ResponseContact) -> None:
        with self._lock:
            self._contacts[(contact.tenant_id, contact.contact_id)] = contact

    def get_contact(self, tenant_id: str, contact_id: str) -> ResponseContact:
        with self._lock:
            contact = self._contacts.get((tenant_id, contact_id))
        if contact is None:
            raise DispatchResourceNotFound()
        return contact

    def resolve(
        self,
        tenant_id: str,
        coverage: CoverageTarget,
        *,
        at: datetime,
    ) -> ResolvedContacts:
        when = require_utc(at, "directory resolution time")
        with self._lock:
            matches = [
                contact
                for contact in self._contacts.values()
                if contact.tenant_id == tenant_id
                and contact.zone_id == coverage.zone_id
                and (
                    not contact.coverage_cells
                    or coverage.cell_id in contact.coverage_cells
                )
                and contact.is_available(when)
            ]

        primary = [item for item in matches if item.role is ContactRole.PRIMARY]
        supervisor = [item for item in matches if item.role is ContactRole.SUPERVISOR]
        if len(primary) > 1 or len(supervisor) > 1:
            raise DispatchContactUnavailable(ambiguous=True)
        if len(primary) != 1 or len(supervisor) != 1:
            raise DispatchContactUnavailable()
        return ResolvedContacts(primary=primary[0], supervisor=supervisor[0])
