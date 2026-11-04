from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .assignment import Candidate
from .models import Technician, Workload

OPEN = "t.status NOT IN ('resolved','closed')"
_SELECT = f"""
SELECT tc.id, tc.name, tc.email_or_username, tc.specialties, tc.zones, tc.level, tc.max_open_tickets,
       tc.availability, tc.schedule, tc.contact_channel, tc.active, tc.data_origin, tc.created_at, tc.updated_at,
       (SELECT count(*) FROM tickets t WHERE t.assigned_technician_id = tc.id AND {OPEN})::int AS open_tickets
FROM technicians tc"""
_COLS = {
    "name", "email_or_username", "specialties", "zones", "level", "max_open_tickets", "availability",
    "schedule", "contact_channel", "active",
}  # fmt: skip


class PgTechnicians:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def list(self, active_only: bool) -> list[Technician]:
        rows = await self._all(_SELECT + (" WHERE tc.active" if active_only else "") + " ORDER BY tc.name")
        return [Technician(**r) for r in rows]

    async def get(self, technician_id: UUID) -> Technician | None:
        rows = await self._all(_SELECT + " WHERE tc.id = %s", (technician_id,))
        return Technician(**rows[0]) if rows else None

    async def create(self, data: dict[str, Any]) -> Technician:
        specialty = data["specialties"][0] if data["specialties"] else None  # legacy single-value column
        rows = await self._all(
            "INSERT INTO technicians (name, email_or_username, specialty, specialties, zones, level, "
            "max_open_tickets, availability, schedule, contact_channel) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (
                data["name"], data["email_or_username"], specialty, data["specialties"], data["zones"],
                data["level"], data["max_open_tickets"], data["availability"], Jsonb(data["schedule"]),
                data["contact_channel"],
            ),
        )  # fmt: skip
        tech = await self.get(rows[0]["id"])
        assert tech is not None
        return tech

    async def update(self, technician_id: UUID, fields: dict[str, Any]) -> Technician | None:
        sets, args = [], []
        for k, v in fields.items():
            if k not in _COLS:
                continue
            sets.append(f"{k} = %s")
            args.append(Jsonb(v) if k == "schedule" else v)
        if "specialties" in fields:
            sets.append("specialty = %s")
            args.append(fields["specialties"][0] if fields["specialties"] else None)
        if sets:
            await self.conn.execute(
                f"UPDATE technicians SET {', '.join(sets)}, updated_at = now() WHERE id = %s", [*args, technician_id]
            )
        return await self.get(technician_id)

    async def workload(self, technician_id: UUID) -> Workload | None:
        tech = await self.get(technician_id)
        if tech is None:
            return None
        rows = await self._all(
            f"SELECT t.ticket_number, t.status, t.sla_due_at FROM tickets t "
            f"WHERE t.assigned_technician_id = %s AND {OPEN} ORDER BY t.sla_due_at NULLS LAST, t.created_at",
            (technician_id,),
        )
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        due = [r["sla_due_at"] for r in rows if r["sla_due_at"]]
        return Workload(
            technician_id=technician_id,
            open_tickets=len(rows),
            max_open_tickets=tech.max_open_tickets,
            by_status=by_status,
            next_sla_due_at=min(due) if due else None,
            tickets=[r["ticket_number"] for r in rows],
        )

    async def candidates(self) -> list[Candidate]:
        rows = await self._all(
            _SELECT.replace(
                "FROM technicians tc",
                ", (SELECT max(e.created_at) FROM ticket_events e WHERE e.event_type IN ('assigned','reassigned') "
                "AND e.new_value->>'technician_id' = tc.id::text) AS last_assigned_at FROM technicians tc",
            )
            + " WHERE tc.active"
        )
        return [
            Candidate(
                id=r["id"], name=r["name"], active=r["active"], availability=r["availability"],
                schedule=r["schedule"], specialties=tuple(r["specialties"]), zones=tuple(r["zones"]),
                level=r["level"], max_open_tickets=r["max_open_tickets"], open_tickets=r["open_tickets"],
                last_assigned_at=r["last_assigned_at"],
            )
            for r in rows
        ]  # fmt: skip
