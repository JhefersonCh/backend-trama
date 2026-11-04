# ruff: noqa: E501
"""Postgres adapter for the media table."""

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

COLS = "id, session_id, kind, mime, path, size_bytes, width, height, analysis, transcript, created_at"


class MediaRepo(Protocol):
    async def session_exists(self, sid: UUID) -> bool: ...
    async def insert(self, row: dict[str, Any]) -> None: ...
    async def get(self, media_id: UUID) -> dict[str, Any] | None: ...
    async def list_for_session(self, sid: UUID, kind: str | None = None) -> list[dict[str, Any]]: ...
    async def retention_days(self) -> int: ...
    async def purge_older_than(self, cutoff: datetime) -> list[str]: ...


class PgMediaRepo:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def session_exists(self, sid: UUID) -> bool:
        return bool(await self._all("SELECT 1 AS x FROM conversation_sessions WHERE id = %s", (sid,)))

    async def insert(self, row: dict[str, Any]) -> None:
        await self._all(
            """INSERT INTO media (id, session_id, kind, mime, path, size_bytes, width, height, analysis, transcript)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING 1""",
            (
                row["id"], row["session_id"], row["kind"], row["mime"], row["path"], row.get("size_bytes"),
                row.get("width"), row.get("height"),
                Jsonb(row["analysis"]) if row.get("analysis") is not None else None, row.get("transcript"),
            ),
        )  # fmt: skip

    async def get(self, media_id: UUID) -> dict[str, Any] | None:
        rows = await self._all(f"SELECT {COLS} FROM media WHERE id = %s", (media_id,))
        return rows[0] if rows else None

    async def list_for_session(self, sid: UUID, kind: str | None = None) -> list[dict[str, Any]]:
        return await self._all(
            f"SELECT {COLS} FROM media WHERE session_id = %s AND (%s::text IS NULL OR kind = %s) ORDER BY created_at",
            (sid, kind, kind),
        )

    async def retention_days(self) -> int:
        rows = await self._all("SELECT value FROM app_settings WHERE key = 'media_retention_days'")
        v = rows[0]["value"] if rows else 30
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 30

    async def purge_older_than(self, cutoff: datetime) -> list[str]:
        rows = await self._all("DELETE FROM media WHERE created_at < %s RETURNING path", (cutoff,))
        return [r["path"] for r in rows]
