# ruff: noqa: E501
"""Postgres adapter for the agent core."""

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

RULE_NAME = "diagnostico_red"


class PgAgentRepo:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    async def session_exists(self, sid: UUID) -> bool:
        return await self._one("SELECT 1 AS x FROM conversation_sessions WHERE id = %s", (sid,)) is not None

    async def get_case(self, sid: UUID) -> dict | None:
        return await self._one(
            """SELECT c.*, t.ticket_number, t.status AS ticket_status FROM cases c
               LEFT JOIN tickets t ON t.id = c.ticket_id WHERE c.session_id = %s""",
            (sid,),
        )

    async def ensure_case(self, sid: UUID) -> dict:
        await self._all("INSERT INTO cases (session_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING 1", (sid,))
        return await self.get_case(sid)  # type: ignore[return-value]

    async def active_facts(self, sid: UUID) -> list[dict]:
        return await self._all(
            """SELECT key, value, source, certainty, evidence_ref, turn_id, created_at FROM case_facts
               WHERE session_id = %s AND superseded_at IS NULL ORDER BY created_at""",
            (sid,),
        )

    async def upsert_fact(self, sid, key, value, source, certainty, evidence_ref=None, turn_id=None) -> dict:
        async with self._pool.connection() as c, c.transaction(), c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE case_facts SET superseded_at = now() WHERE session_id = %s AND key = %s AND superseded_at IS NULL",
                (sid, key),
            )
            await cur.execute(
                """INSERT INTO case_facts (session_id, key, value, source, certainty, evidence_ref, turn_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   RETURNING key, value, source, certainty, evidence_ref, turn_id, created_at""",
                (sid, key, Jsonb(value), source, certainty, evidence_ref, turn_id),
            )
            return await cur.fetchone()

    async def get_hypotheses(self, sid: UUID) -> list[dict]:
        return await self._all(
            "SELECT key, cf, status FROM case_hypotheses WHERE session_id = %s ORDER BY cf DESC, key", (sid,)
        )

    async def replace_hypotheses(self, sid: UUID, items: list[dict]) -> None:
        async with self._pool.connection() as c, c.transaction():
            await c.execute("DELETE FROM case_hypotheses WHERE session_id = %s", (sid,))
            for h in items:
                await c.execute(
                    "INSERT INTO case_hypotheses (session_id, key, cf, status) VALUES (%s,%s,%s,%s)",
                    (sid, h["key"], h["cf"], h["status"]),
                )

    async def append_trace(self, sid, phase, *, facts_used, rules_fired, hypotheses, action=None, autonomy=None,
                           reason=None, turn_id=None) -> None:  # fmt: skip
        await self._all(
            """INSERT INTO decision_trace
               (session_id, turn_id, phase, facts_used, rules_fired, hypotheses, action, autonomy, reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING 1""",
            (sid, turn_id, phase, Jsonb(facts_used), rules_fired, Jsonb(hypotheses), action, autonomy, reason),
        )

    async def list_trace(self, sid: UUID, limit: int = 50) -> list[dict]:
        rows = await self._all(
            """SELECT id, turn_id, phase, facts_used, rules_fired, hypotheses, action, autonomy, reason, created_at
               FROM decision_trace WHERE session_id = %s ORDER BY created_at DESC LIMIT %s""",
            (sid, limit),
        )
        return rows[::-1]

    async def latest_trace(self, sid: UUID, phase: str) -> dict | None:
        return await self._one(
            """SELECT id, turn_id, phase, facts_used, rules_fired, hypotheses, action, autonomy, reason, created_at
               FROM decision_trace WHERE session_id = %s AND phase = %s ORDER BY created_at DESC LIMIT 1""",
            (sid, phase),
        )

    async def actions_taken(self, sid: UUID) -> list[str]:
        """Executed actions ('actuar' rows), oldest first (PART 2: select_action never repeats a step)."""
        rows = await self._all(
            """SELECT action FROM decision_trace WHERE session_id = %s AND phase = 'actuar' AND action IS NOT NULL
               ORDER BY created_at""",
            (sid,),
        )
        return [r["action"] for r in rows]

    async def update_case(
        self, sid: UUID, *, state: str | None = None, verified: bool | None = None, ticket_number: str | None = None
    ) -> None:
        await self._all(
            """UPDATE cases SET state = coalesce(%s::text, state), verified = coalesce(%s::boolean, verified),
                 resolved_at = CASE WHEN %s::boolean IS TRUE AND resolved_at IS NULL THEN now() ELSE resolved_at END,
                 ticket_id = coalesce((SELECT id FROM tickets WHERE ticket_number = %s::text), ticket_id),
                 updated_at = now()
               WHERE session_id = %s RETURNING 1""",
            (state, verified, verified, ticket_number, sid),
        )

    async def get_setting(self, key: str) -> Any | None:
        row = await self._one("SELECT value FROM app_settings WHERE key = %s", (key,))
        return row["value"] if row else None

    async def active_rule_set(self) -> dict | None:
        return await self._one(
            "SELECT version, name, content_yaml, is_active, created_at FROM rule_sets WHERE name = %s AND is_active",
            (RULE_NAME,),
        )

    async def list_rule_sets(self) -> list[dict]:
        return await self._all(
            "SELECT version, name, is_active, created_at FROM rule_sets WHERE name = %s ORDER BY version DESC",
            (RULE_NAME,),
        )

    async def get_rule_set(self, version: int) -> dict | None:
        return await self._one(
            "SELECT version, name, content_yaml, is_active, created_at FROM rule_sets WHERE name = %s AND version = %s",
            (RULE_NAME, version),
        )

    async def create_rule_set(self, content_yaml: str) -> dict:
        async with self._pool.connection() as c, c.transaction(), c.cursor(row_factory=dict_row) as cur:
            await cur.execute("LOCK TABLE rule_sets IN SHARE ROW EXCLUSIVE MODE")
            await cur.execute(
                """INSERT INTO rule_sets (name, version, content_yaml, is_active)
                   SELECT %s, coalesce(max(version), 0) + 1, %s, false FROM rule_sets WHERE name = %s
                   RETURNING version, name, content_yaml, is_active, created_at""",
                (RULE_NAME, content_yaml, RULE_NAME),
            )
            return await cur.fetchone()

    async def activate_rule_set(self, version: int) -> None:
        async with self._pool.connection() as c, c.transaction():
            await c.execute("UPDATE rule_sets SET is_active = false WHERE name = %s AND is_active", (RULE_NAME,))
            await c.execute(
                "UPDATE rule_sets SET is_active = true WHERE name = %s AND version = %s", (RULE_NAME, version)
            )
