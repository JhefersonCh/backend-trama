from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.technicians.ports import TechnicianRepo

from .models import TicketDetail, TicketEvent, TicketSummary


class TicketRepo(Protocol):
    async def create(
        self,
        *,
        title: str,
        description: str,
        category: str | None,
        priority: str,
        session_id: str | None,
        source: str,
        zone: str | None = None,
        service_mode: str | None = None,
        preferred_slot: str | None = None,
        handoff_summary: dict | None = None,
    ) -> TicketSummary: ...
    async def list(
        self, status: str | None, limit: int, technician_id: UUID | None = None, unassigned: bool = False
    ) -> list[TicketSummary]: ...
    async def get(self, ticket_number: str, for_update: bool = False) -> TicketDetail | None: ...
    async def update(self, ticket_number: str, fields: dict[str, Any]) -> None:
        """Updates ticket (status/priority; closed_at on resolved/closed) and keeps the incident status in sync."""
        ...

    async def add_event(
        self, ticket_number: str, event_type: str, old: Any, new: Any, details: str | None = None
    ) -> TicketEvent: ...

    async def set_assignment(self, ticket_number: str, technician_id: UUID, status: str) -> None: ...
    async def set_sla(self, ticket_number: str, sla_due_at: datetime | None) -> None: ...
    async def continuity_technician(self, ticket_number: str, now: datetime) -> UUID | None:
        """Technician of the same person's ticket closed < 30 days ago (same session_id / reporter_ref)."""
        ...

    async def find_for_session(self, session_id: str) -> str | None:
        """Ticket number: the session's ticket, else the most recent one of the same reporter_ref."""
        ...

    async def settings(self, keys: list[str]) -> dict[str, Any]: ...
    async def save_contact(self, ticket_number: str, ciphertext: bytes) -> None: ...
    async def notify_session(self, session_id: UUID, text: str, kind: str) -> bool:
        """Insert into pending_notifications if that table exists. Returns whether it was inserted."""
        ...


class TicketUnitOfWork(Protocol):
    """One DB transaction: commit on clean exit, rollback on exception."""

    tickets: TicketRepo
    technicians: TechnicianRepo

    async def __aenter__(self) -> TicketUnitOfWork: ...
    async def __aexit__(self, *exc: object) -> None: ...
