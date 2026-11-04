import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config.errors import ConflictError, DomainValidationError, NotFoundError
from app.shared.crypto import SecretCipher
from app.technicians.assignment import STRATEGIES, TicketInfo, decide, sla_due

from .models import LastNote, TicketDetail, TicketPatch, TicketSummary, TicketTimeline, TimelineTechnician
from .ports import TicketUnitOfWork

PRIORITIES = {"low", "medium", "high", "critical"}
CLOSED = ("resolved", "closed")
DEFAULT_SLA = {"critical": 4, "high": 8, "medium": 24, "low": 72}


def _description_with_evidence(description: str, evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return description
    lines = "\n".join("- " + json.dumps(e, ensure_ascii=False, default=str) for e in evidence)
    return f"{description}\n\nEvidencias:\n{lines}".strip()


class TicketService:
    def __init__(
        self,
        uow_factory: Callable[[], TicketUnitOfWork],
        cipher: SecretCipher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow = uow_factory
        self._cipher = cipher
        self._now = clock

    async def create_incident_with_ticket(
        self,
        *,
        title: str,
        description: str,
        category: str | None,
        priority: str,
        session_id: str | None,
        evidence: list[dict],
        source: str = "conversation",
        zone: str | None = None,
        service_mode: str | None = None,
        preferred_slot: str | None = None,
        handoff_summary: dict | None = None,
        auto_assign: bool = True,
    ) -> TicketSummary:
        if priority not in PRIORITIES:
            raise DomainValidationError(f"priority must be one of {sorted(PRIORITIES)}")
        async with self._uow() as uow:  # incident + ticket + 'created' event: one transaction
            summary = await uow.tickets.create(
                title=title,
                description=_description_with_evidence(description, evidence),
                category=category,
                priority=priority,
                session_id=session_id,
                source=source,
                zone=zone,
                service_mode=service_mode,
                preferred_slot=preferred_slot,
                handoff_summary=handoff_summary,
            )
            await uow.tickets.add_event(summary.ticket_number, "created", None, {"status": "open"}, "ticket created")
            if not auto_assign:
                return summary
            reason, queued = await self._assign_auto(uow, summary.ticket_number)
            fresh = await uow.tickets.get(summary.ticket_number)
            assert fresh is not None
            return summary.model_copy(
                update={
                    "status": fresh.status,
                    "technician_name": fresh.technician_name,
                    "sla_due_at": fresh.sla_due_at,
                    "assignment_reason": reason,
                    "queued": queued,
                }
            )

    async def _assign_auto(self, uow: TicketUnitOfWork, ticket_number: str) -> tuple[str, bool]:
        """Run K2 for the ticket, persist the outcome (events, status, SLA). Returns (reason, queued)."""
        t = await uow.tickets.get(ticket_number, for_update=True)
        assert t is not None
        cfg = await uow.tickets.settings(["assignment_strategy", "sla_hours"])
        strategy = cfg.get("assignment_strategy")
        strategy = strategy if strategy in STRATEGIES else "menor_carga"
        sla_hours = cfg.get("sla_hours") if isinstance(cfg.get("sla_hours"), dict) else DEFAULT_SLA
        now = self._now()
        if t.sla_due_at is None:
            await uow.tickets.set_sla(ticket_number, sla_due(t.created_at, t.priority, sla_hours))
        decision = decide(
            TicketInfo(t.priority, t.category, t.zone, t.service_mode),
            await uow.technicians.candidates(),
            strategy,
            now,
            continuity_id=await uow.tickets.continuity_technician(ticket_number, now),
        )
        if decision.technician_id is None:
            if t.assigned_technician_id is None:  # keep an existing assignment when a re-run finds nobody
                await uow.tickets.add_event(ticket_number, "unassigned_queue", None, None, decision.reason)
            return decision.reason, t.assigned_technician_id is None
        if decision.technician_id != t.assigned_technician_id:
            await self._persist_assignment(uow, t, decision.technician_id, decision.technician_name, decision.reason)
        return decision.reason, False

    async def _persist_assignment(
        self, uow: TicketUnitOfWork, t: TicketDetail, tech_id: UUID, tech_name: str | None, reason: str
    ) -> None:
        previous = t.assigned_technician_id
        status = t.status if t.status == "in_progress" else "assigned"
        await uow.tickets.set_assignment(t.ticket_number, tech_id, status)
        await uow.tickets.add_event(
            t.ticket_number,
            "reassigned" if previous else "assigned",
            {"technician_id": str(previous)} if previous else None,
            {"technician_id": str(tech_id), "technician_name": tech_name},
            reason,
        )

    async def assign(self, ticket_number: str, technician_id: UUID | None, auto: bool) -> TicketTimeline:
        async with self._uow() as uow:
            t = await uow.tickets.get(ticket_number, for_update=True)
            if t is None:
                raise NotFoundError("ticket not found")
            if t.status in CLOSED:
                raise ConflictError("ticket is already resolved or closed")
            if auto:
                await self._assign_auto(uow, ticket_number)
            else:
                assert technician_id is not None
                tech = await uow.technicians.get(technician_id)
                if tech is None:
                    raise NotFoundError("technician not found")
                if not tech.active:
                    raise ConflictError("technician is inactive")
                if technician_id != t.assigned_technician_id:
                    await self._persist_assignment(uow, t, technician_id, tech.name, "asignación manual")
                if t.sla_due_at is None:
                    cfg = await uow.tickets.settings(["sla_hours"])
                    hours = cfg.get("sla_hours") if isinstance(cfg.get("sla_hours"), dict) else DEFAULT_SLA
                    await uow.tickets.set_sla(ticket_number, sla_due(t.created_at, t.priority, hours))
            return await self._timeline(uow, ticket_number)

    async def _timeline(self, uow: TicketUnitOfWork, ticket_number: str) -> TicketTimeline:
        t = await uow.tickets.get(ticket_number)
        if t is None:
            raise NotFoundError("ticket not found")
        tech = await uow.technicians.get(t.assigned_technician_id) if t.assigned_technician_id else None
        now = self._now()
        notes = [e for e in t.events if e.event_type == "technician_note"]
        last = notes[-1] if notes else None
        nv = (last.new_value or {}) if last else {}
        return TicketTimeline(
            ticket_number=t.ticket_number,
            status=t.status,
            priority=t.priority,
            title=t.title,
            created_at=t.created_at,
            opened_at=t.created_at,
            session_id=t.session_id,
            age_seconds=max(0, int((now - t.created_at).total_seconds())),
            technician=TimelineTechnician(id=tech.id, name=tech.name, specialties=tech.specialties) if tech else None,
            queued=tech is None and any(e.event_type == "unassigned_queue" for e in t.events),
            sla_due_at=t.sla_due_at,
            sla_remaining_seconds=int((t.sla_due_at - now).total_seconds()) if t.sla_due_at else None,
            handoff_summary=t.handoff_summary,
            last_note=LastNote(text=nv.get("text", ""), technician_name=nv.get("technician_name"), at=last.created_at)
            if last
            else None,
            has_contact=t.has_contact,
            events=t.events,
        )

    async def timeline(self, ticket_number: str) -> TicketTimeline:
        async with self._uow() as uow:
            return await self._timeline(uow, ticket_number)

    async def mine(self, session_id: str) -> TicketTimeline:
        """A1 data source: the session's ticket, else the most recent one of the same reporter_ref."""
        async with self._uow() as uow:
            number = await uow.tickets.find_for_session(session_id)
            if number is None:
                raise NotFoundError("no ticket for this session")
            return await self._timeline(uow, number)

    async def add_note(self, ticket_number: str, technician_id: UUID, text: str) -> TicketTimeline:
        async with self._uow() as uow:
            t = await uow.tickets.get(ticket_number, for_update=True)
            if t is None:
                raise NotFoundError("ticket not found")
            tech = await uow.technicians.get(technician_id)
            if tech is None:
                raise NotFoundError("technician not found")
            await uow.tickets.add_event(
                ticket_number,
                "technician_note",
                None,
                {"text": text, "technician_id": str(technician_id), "technician_name": tech.name},
                f"nota de {tech.name}",
            )
            if t.session_id:
                await uow.tickets.notify_session(
                    t.session_id, f"{tech.name.split()[0]} dejó una nota en su caso: {text}", "ticket_update"
                )
            return await self._timeline(uow, ticket_number)

    async def set_contact(self, ticket_number: str, value: str) -> TicketTimeline:
        if self._cipher is None:
            raise DomainValidationError("contact storage unavailable")
        async with self._uow() as uow:
            if await uow.tickets.get(ticket_number) is None:
                raise NotFoundError("ticket not found")
            await uow.tickets.save_contact(ticket_number, self._cipher.encrypt(value))
            await uow.tickets.add_event(ticket_number, "contact_registered", None, None, "contacto registrado")
            return await self._timeline(uow, ticket_number)

    async def list_tickets(
        self, status: str | None, limit: int, technician_id: UUID | None = None, unassigned: bool = False
    ) -> list[TicketSummary]:
        async with self._uow() as uow:
            return await uow.tickets.list(status, limit, technician_id, unassigned)

    async def get_ticket(self, ticket_number: str) -> TicketSummary | None:
        async with self._uow() as uow:
            detail = await uow.tickets.get(ticket_number)
        return TicketSummary(**detail.model_dump(include=set(TicketSummary.model_fields))) if detail else None

    async def get_detail(self, ticket_number: str) -> TicketDetail:
        async with self._uow() as uow:
            detail = await uow.tickets.get(ticket_number)
        if detail is None:
            raise NotFoundError("ticket not found")
        return detail

    async def patch(self, ticket_number: str, body: TicketPatch) -> TicketDetail:
        async with self._uow() as uow:
            current = await uow.tickets.get(ticket_number, for_update=True)
            if current is None:
                raise NotFoundError("ticket not found")
            changes = {k: v for k, v in body.model_dump(exclude_none=True).items() if getattr(current, k) != v}
            if changes:
                await uow.tickets.update(ticket_number, changes)
                for field, new in changes.items():
                    await uow.tickets.add_event(
                        ticket_number, f"{field}_changed", getattr(current, field), new, f"{field} changed"
                    )
            detail = await uow.tickets.get(ticket_number)
        assert detail is not None
        return detail
