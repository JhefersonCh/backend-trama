from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row


def _s(r: dict) -> dict:
    return {k: (str(v) if k.endswith("id") and v is not None else v) for k, v in r.items()}


class PgProactiveStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: Any = ()) -> list[dict]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return [_s(r) for r in await cur.fetchall()] if cur.description else []

    async def insert_job(self, kind, session_id, ticket_id, run_at: datetime, payload) -> str:
        rows = await self._all(
            "INSERT INTO scheduled_jobs (kind, session_id, ticket_id, run_at, payload) "
            "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
            (kind, session_id, ticket_id, run_at, json.dumps(payload)),
        )
        return rows[0]["id"]

    async def claim_due(self, limit: int) -> list[dict]:
        async with self._pool.connection() as c, c.transaction(), c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id FROM scheduled_jobs WHERE status = 'pending' AND run_at <= now() "
                "ORDER BY run_at LIMIT %s FOR UPDATE SKIP LOCKED",
                (limit,),
            )
            ids = [r["id"] for r in await cur.fetchall()]
            if not ids:
                return []
            await cur.execute(
                "UPDATE scheduled_jobs SET status = 'running', attempts = attempts + 1 WHERE id = ANY(%s) "
                "RETURNING id, kind, session_id, ticket_id, run_at, payload, attempts",
                (ids,),
            )
            return [_s(r) for r in await cur.fetchall()]

    async def finish_job(self, job_id, status, error, run_at=None) -> None:
        async with self._pool.connection() as c:
            await c.execute(
                "UPDATE scheduled_jobs SET status = %s, last_error = %s, run_at = coalesce(%s, run_at) WHERE id = %s",
                (status, error, run_at, job_id),
            )

    async def reset_running(self) -> None:
        async with self._pool.connection() as c:
            await c.execute("UPDATE scheduled_jobs SET status = 'pending' WHERE status = 'running'")

    async def list_jobs(self, status, limit) -> list[dict]:
        return await self._all(
            "SELECT id, kind, session_id, ticket_id, run_at, payload, status, attempts, last_error, created_at "
            "FROM scheduled_jobs WHERE (%s::text IS NULL OR status = %s) ORDER BY run_at DESC LIMIT %s",
            (status, status, limit),
        )

    async def insert_notification(self, session_id, text, kind) -> str:
        rows = await self._all(
            "INSERT INTO pending_notifications (session_id, text, kind) VALUES (%s, %s, %s) RETURNING id",
            (session_id, text, kind),
        )
        return rows[0]["id"]

    async def undelivered(self, session_id) -> list[dict]:
        return await self._all(
            "SELECT id, kind, text FROM pending_notifications WHERE session_id = %s AND delivered_at IS NULL "
            "ORDER BY created_at",
            (session_id,),
        )

    async def mark_delivered(self, notification_id) -> None:
        async with self._pool.connection() as c:
            await c.execute(
                "UPDATE pending_notifications SET delivered_at = now() WHERE id = %s AND delivered_at IS NULL",
                (notification_id,),
            )

    async def list_notifications(self, session_id, limit) -> list[dict]:
        return await self._all(
            "SELECT id, kind, text, delivered_at, created_at FROM pending_notifications "
            "WHERE session_id = %s ORDER BY created_at DESC LIMIT %s",
            (session_id, limit),
        )

    async def ticket_info(self, ticket_id) -> dict | None:
        rows = await self._all(
            "SELECT t.id, t.ticket_number, t.status, t.created_at, t.sla_due_at, t.session_id, "
            "tech.name AS technician_name FROM tickets t "
            "LEFT JOIN technicians tech ON tech.id = t.assigned_technician_id WHERE t.id = %s",
            (ticket_id,),
        )
        return rows[0] if rows else None

    async def open_ticket_for_session(self, session_id) -> dict | None:
        rows = await self._all(
            "SELECT ticket_number FROM tickets WHERE session_id = %s AND status NOT IN ('resolved','closed') "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        return rows[0] if rows else None

    async def add_ticket_event(self, ticket_id, event_type, new, details) -> None:
        async with self._pool.connection() as c:
            await c.execute(
                "INSERT INTO ticket_events (ticket_id, event_type, new_value, details) VALUES (%s, %s, %s::jsonb, %s)",
                (ticket_id, event_type, json.dumps(new) if new is not None else None, details),
            )
