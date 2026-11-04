from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.technicians.pg_repo import PgTechnicians

from .models import TicketDetail, TicketEvent, TicketSummary

_SELECT = """
SELECT t.ticket_number, t.status, t.priority, i.title, t.created_at, t.updated_at,
       t.id AS ticket_id, i.id AS incident_id, i.description, i.category, i.source_type,
       i.reporter_ref, t.closed_at, t.assigned_technician_id,
       t.assigned_technician_id AS technician_id, tc.name AS technician_name, t.sla_due_at,
       t.session_id, t.zone, t.service_mode, t.preferred_slot, t.handoff_summary,
       EXISTS (SELECT 1 FROM ticket_contacts k WHERE k.ticket_id = t.id) AS has_contact
FROM tickets t JOIN incidents i ON i.id = t.incident_id
LEFT JOIN technicians tc ON tc.id = t.assigned_technician_id"""
_INCIDENT_STATUS = {"open": "open", "in_progress": "investigating", "resolved": "resolved", "closed": "closed"}


def _j(v: Any) -> Any:
    return None if v is None else Jsonb(v)


class PgTickets:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

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
    ) -> TicketSummary:
        inc = await self._all(
            "INSERT INTO incidents (title, description, category, priority, reporter_ref, source_type) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (title, description, category, priority, session_id, source),
        )
        rows = await self._all(
            "INSERT INTO tickets (incident_id, priority, category, zone, service_mode, preferred_slot, "
            "handoff_summary, session_id) VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "(SELECT id FROM conversation_sessions WHERE id::text = %s)) "
            "RETURNING ticket_number, status, priority, created_at, updated_at",
            (inc[0]["id"], priority, category, zone, service_mode, preferred_slot, _j(handoff_summary), session_id),
        )
        return TicketSummary(title=title, **rows[0])

    async def list(
        self, status: str | None, limit: int, technician_id: UUID | None = None, unassigned: bool = False
    ) -> list[TicketSummary]:
        where, args = [], []
        if status:
            where.append("t.status = %s")
            args.append(status)
        if technician_id:
            where.append("t.assigned_technician_id = %s")
            args.append(technician_id)
        if unassigned:
            where.append("t.assigned_technician_id IS NULL AND t.status NOT IN ('resolved','closed')")
        sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "")
        rows = await self._all(sql + " ORDER BY t.created_at DESC LIMIT %s", [*args, limit])
        return [TicketSummary(**{k: r[k] for k in TicketSummary.model_fields if k in r}) for r in rows]

    async def get(self, ticket_number: str, for_update: bool = False) -> TicketDetail | None:
        rows = await self._all(
            _SELECT + " WHERE t.ticket_number = %s" + (" FOR UPDATE OF t" if for_update else ""), (ticket_number,)
        )
        if not rows:
            return None
        r = rows[0]
        ev = await self._all(
            "SELECT id, event_type, old_value, new_value, details, created_at FROM ticket_events "
            "WHERE ticket_id = %s ORDER BY created_at, id",
            (r.pop("ticket_id"),),
        )
        return TicketDetail(**r, events=[TicketEvent(**e) for e in ev])

    async def set_assignment(self, ticket_number: str, technician_id: UUID, status: str) -> None:
        await self.conn.execute(
            "UPDATE tickets SET assigned_technician_id = %s, status = %s WHERE ticket_number = %s",
            (technician_id, status, ticket_number),
        )

    async def set_sla(self, ticket_number: str, sla_due_at: datetime | None) -> None:
        await self.conn.execute(
            "UPDATE tickets SET sla_due_at = %s WHERE ticket_number = %s", (sla_due_at, ticket_number)
        )

    async def continuity_technician(self, ticket_number: str, now: datetime) -> UUID | None:
        rows = await self._all(
            "SELECT o.assigned_technician_id FROM tickets cur JOIN incidents ci ON ci.id = cur.incident_id "
            "JOIN tickets o ON o.id <> cur.id AND o.assigned_technician_id IS NOT NULL "
            "AND o.status IN ('resolved','closed') AND o.closed_at > %s "
            "JOIN incidents oi ON oi.id = o.incident_id "
            "WHERE cur.ticket_number = %s AND ((cur.session_id IS NOT NULL AND o.session_id = cur.session_id) "
            "OR (ci.reporter_ref IS NOT NULL AND oi.reporter_ref = ci.reporter_ref)) "
            "ORDER BY o.closed_at DESC LIMIT 1",
            (now - timedelta(days=30), ticket_number),
        )
        return rows[0]["assigned_technician_id"] if rows else None

    async def find_for_session(self, session_id: str) -> str | None:
        rows = await self._all(
            "SELECT t.ticket_number FROM tickets t JOIN incidents i ON i.id = t.incident_id "
            "WHERE t.session_id::text = %s OR i.reporter_ref = %s "
            "ORDER BY (t.session_id::text = %s) DESC, t.created_at DESC LIMIT 1",
            (session_id, session_id, session_id),
        )
        return rows[0]["ticket_number"] if rows else None

    async def settings(self, keys: list[str]) -> dict[str, Any]:
        rows = await self._all("SELECT key, value FROM app_settings WHERE key = ANY(%s)", (keys,))
        return {r["key"]: r["value"] for r in rows}

    async def save_contact(self, ticket_number: str, ciphertext: bytes) -> None:
        await self.conn.execute(
            "INSERT INTO ticket_contacts (ticket_id, ciphertext) SELECT id, %s FROM tickets WHERE ticket_number = %s "
            "ON CONFLICT (ticket_id) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, consent_at = now()",
            (ciphertext, ticket_number),
        )

    async def notify_session(self, session_id: UUID, text: str, kind: str) -> bool:
        rows = await self._all("SELECT to_regclass('public.pending_notifications') IS NOT NULL AS ok")
        if not rows[0]["ok"]:
            return False
        await self.conn.execute(
            "INSERT INTO pending_notifications (session_id, text, kind) "
            "SELECT id, %s, %s FROM conversation_sessions WHERE id = %s",
            (text, kind, session_id),
        )
        return True

    async def update(self, ticket_number: str, fields: dict[str, Any]) -> None:
        sets, args = [], []
        for k in ("status", "priority"):
            if k in fields:
                sets.append(f"{k} = %s")
                args.append(fields[k])
        if "status" in fields:
            sets.append("closed_at = CASE WHEN %s IN ('resolved','closed') THEN now() ELSE NULL END")
            args.append(fields["status"])
        rows = await self._all(
            f"UPDATE tickets SET {', '.join(sets)} WHERE ticket_number = %s RETURNING incident_id",
            [*args, ticket_number],
        )
        inc_sets, inc_args = [], []
        if "status" in fields:
            inc_sets.append("status = %s")
            inc_args.append(_INCIDENT_STATUS[fields["status"]])
        if "priority" in fields:
            inc_sets.append("priority = %s")
            inc_args.append(fields["priority"])
        if rows and inc_sets:
            await self.conn.execute(
                f"UPDATE incidents SET {', '.join(inc_sets)} WHERE id = %s", [*inc_args, rows[0]["incident_id"]]
            )

    async def add_event(
        self, ticket_number: str, event_type: str, old: Any, new: Any, details: str | None = None
    ) -> TicketEvent:
        rows = await self._all(
            "INSERT INTO ticket_events (ticket_id, event_type, old_value, new_value, details) "
            "SELECT id, %s, %s, %s, %s FROM tickets WHERE ticket_number = %s "
            "RETURNING id, event_type, old_value, new_value, details, created_at",
            (event_type, _j(old), _j(new), details, ticket_number),
        )
        return TicketEvent(**rows[0])


class PgTicketUnitOfWork:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> PgTicketUnitOfWork:
        self._cm = self._pool.connection()
        conn = await self._cm.__aenter__()
        self.tickets = PgTickets(conn)
        self.technicians = PgTechnicians(conn)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._cm.__aexit__(*exc)
