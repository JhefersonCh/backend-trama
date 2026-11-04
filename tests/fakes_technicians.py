"""In-memory ticket + technician unit of work (mirrors PgTickets/PgTechnicians semantics for endpoint tests)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.technicians.assignment import Candidate
from app.technicians.models import Technician, Workload
from app.tickets.models import TicketDetail, TicketEvent, TicketSummary

CLOSED = ("resolved", "closed")


def _now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, settings: dict | None = None, now: datetime | None = None):
        self.now = now or _now()
        self.rows: dict[str, dict] = {}
        self.events: dict[str, list[TicketEvent]] = {}
        self.techs: dict[UUID, dict] = {}
        self.contacts: dict[str, bytes] = {}
        self.notifications: list[tuple[UUID, str, str]] = []
        self.settings: dict = settings or {}


class _Tickets:
    def __init__(self, s: Store):
        self.s = s

    def _view(self, r: dict) -> dict:
        tech = self.s.techs.get(r["assigned_technician_id"]) if r["assigned_technician_id"] else None
        return {
            **r,
            "technician_id": r["assigned_technician_id"],
            "technician_name": tech["name"] if tech else None,
            "has_contact": r["ticket_number"] in self.s.contacts,
        }

    async def create(self, *, title, description, category, priority, session_id, source, zone=None,
                     service_mode=None, preferred_slot=None, handoff_summary=None):  # fmt: skip
        n = f"TRM-{len(self.s.rows) + 1:06d}"
        now = self.s.now
        try:
            sid = UUID(session_id) if session_id else None
        except ValueError:
            sid = None
        self.s.rows[n] = dict(
            ticket_number=n, status="open", priority=priority, title=title, created_at=now, updated_at=now,
            incident_id=uuid4(), description=description, category=category, source_type=source,
            reporter_ref=session_id, closed_at=None, assigned_technician_id=None, sla_due_at=None, session_id=sid,
            zone=zone, service_mode=service_mode, preferred_slot=preferred_slot, handoff_summary=handoff_summary,
        )  # fmt: skip
        self.s.events[n] = []
        return TicketSummary(
            **{k: self._view(self.s.rows[n])[k] for k in TicketSummary.model_fields if k in self.s.rows[n]}
        )

    async def list(self, status, limit, technician_id=None, unassigned=False):
        out = []
        for r in self.s.rows.values():
            if status and r["status"] != status:
                continue
            if technician_id and r["assigned_technician_id"] != technician_id:
                continue
            if unassigned and (r["assigned_technician_id"] or r["status"] in CLOSED):
                continue
            v = self._view(r)
            out.append(TicketSummary(**{k: v[k] for k in TicketSummary.model_fields if k in v}))
        return out[:limit]

    async def get(self, ticket_number, for_update=False):
        r = self.s.rows.get(ticket_number)
        return TicketDetail(**self._view(r), events=list(self.s.events[ticket_number])) if r else None

    async def update(self, ticket_number, fields):
        self.s.rows[ticket_number].update(fields)

    async def add_event(self, ticket_number, event_type, old, new, details=None):
        e = TicketEvent(
            id=uuid4(), event_type=event_type, old_value=old, new_value=new, details=details, created_at=_now()
        )
        self.s.events[ticket_number].append(e)
        return e

    async def set_assignment(self, ticket_number, technician_id, status):
        self.s.rows[ticket_number].update(assigned_technician_id=technician_id, status=status)

    async def set_sla(self, ticket_number, sla_due_at):
        self.s.rows[ticket_number]["sla_due_at"] = sla_due_at

    async def continuity_technician(self, ticket_number, now):
        cur = self.s.rows[ticket_number]
        for r in self.s.rows.values():
            if (
                r is not cur and r["assigned_technician_id"] and r["status"] in CLOSED and r["closed_at"]
                and r["closed_at"] > now - timedelta(days=30)
                and ((cur["session_id"] and r["session_id"] == cur["session_id"])
                     or (cur["reporter_ref"] and r["reporter_ref"] == cur["reporter_ref"]))
            ):  # fmt: skip
                return r["assigned_technician_id"]
        return None

    async def find_for_session(self, session_id):
        rows = [
            r for r in self.s.rows.values() if str(r["session_id"]) == session_id or r["reporter_ref"] == session_id
        ]
        rows.sort(key=lambda r: r["created_at"])
        return rows[-1]["ticket_number"] if rows else None

    async def settings(self, keys):
        return {k: v for k, v in self.s.settings.items() if k in keys}

    async def save_contact(self, ticket_number, ciphertext):
        self.s.contacts[ticket_number] = ciphertext

    async def notify_session(self, session_id, text, kind):
        self.s.notifications.append((session_id, text, kind))
        return True


class _Technicians:
    def __init__(self, s: Store):
        self.s = s

    def _open(self, tid: UUID) -> list[dict]:
        return [r for r in self.s.rows.values() if r["assigned_technician_id"] == tid and r["status"] not in CLOSED]

    def _model(self, tid: UUID) -> Technician:
        return Technician(**self.s.techs[tid], open_tickets=len(self._open(tid)))

    async def list(self, active_only):
        return [self._model(i) for i, t in self.s.techs.items() if t["active"] or not active_only]

    async def get(self, technician_id):
        return self._model(technician_id) if technician_id in self.s.techs else None

    async def create(self, data):
        tid = uuid4()
        self.s.techs[tid] = {**data, "id": tid, "active": True, "data_origin": "real", "created_at": _now()}
        return self._model(tid)

    async def update(self, technician_id, fields):
        if technician_id not in self.s.techs:
            return None
        self.s.techs[technician_id].update(fields)
        return self._model(technician_id)

    async def workload(self, technician_id):
        if technician_id not in self.s.techs:
            return None
        rows = self._open(technician_id)
        by: dict[str, int] = {}
        for r in rows:
            by[r["status"]] = by.get(r["status"], 0) + 1
        due = [r["sla_due_at"] for r in rows if r["sla_due_at"]]
        return Workload(
            technician_id=technician_id,
            open_tickets=len(rows),
            max_open_tickets=self.s.techs[technician_id]["max_open_tickets"],
            by_status=by,
            next_sla_due_at=min(due) if due else None,
            tickets=[r["ticket_number"] for r in rows],
        )

    async def candidates(self):
        out = []
        for tid, t in self.s.techs.items():
            if not t["active"]:
                continue
            stamps = [
                e.created_at for evs in self.s.events.values() for e in evs
                if e.event_type in ("assigned", "reassigned") and (e.new_value or {}).get("technician_id") == str(tid)
            ]  # fmt: skip
            out.append(
                Candidate(
                    id=tid,
                    name=t["name"],
                    availability=t["availability"],
                    schedule=t["schedule"],
                    specialties=tuple(t["specialties"]),
                    zones=tuple(t["zones"]),
                    level=t["level"],
                    max_open_tickets=t["max_open_tickets"],
                    open_tickets=len(self._open(tid)),
                    last_assigned_at=max(stamps) if stamps else None,
                )  # fmt: skip
            )
        return out


class FakeUoW:
    def __init__(self, s: Store):
        self.tickets = _Tickets(s)
        self.technicians = _Technicians(s)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass
