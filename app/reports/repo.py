"""Read-only SQL for the report (sources cited in the conversation, media, session)."""

import json
from typing import Any, Protocol
from uuid import UUID

from psycopg.rows import dict_row


class ReportRepo(Protocol):
    async def session(self, sid: UUID) -> dict[str, Any] | None: ...
    async def tool_payloads(self, sid: UUID) -> list[str]: ...
    async def photos(self, sid: UUID) -> list[dict[str, Any]]: ...


class PgReportRepo:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def session(self, sid: UUID) -> dict[str, Any] | None:
        rows = await self._all("SELECT id, started_at, title FROM conversation_sessions WHERE id = %s", (sid,))
        return rows[0] if rows else None

    async def tool_payloads(self, sid: UUID) -> list[str]:
        rows = await self._all(
            "SELECT content FROM conversation_messages WHERE session_id = %s AND role = 'tool' ORDER BY created_at",
            (sid,),
        )
        return [r["content"] for r in rows]

    async def photos(self, sid: UUID) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT id, path, analysis, created_at FROM media WHERE session_id = %s AND kind = 'image' "
            "ORDER BY created_at",
            (sid,),
        )


def parse_sources(payloads: list[str], limit: int = 8) -> list[dict[str, Any]]:
    """Distinct cited sources (title+uri+section) with their excerpt, in the order they were first used."""
    seen: dict[tuple, dict[str, Any]] = {}
    for raw in payloads:
        try:
            items = json.loads(raw).get("sources") or []
        except (ValueError, AttributeError):
            continue
        for s in items:
            if not isinstance(s, dict) or not s.get("title"):
                continue
            key = (s.get("title"), s.get("source_uri"), s.get("section"))
            if key not in seen and len(seen) < limit:
                seen[key] = {
                    "title": s["title"], "section": s.get("section"), "source_uri": s.get("source_uri"),
                    "excerpt": s.get("excerpt"), "is_synthetic": bool(s.get("is_synthetic")),
                }  # fmt: skip
    return list(seen.values())
