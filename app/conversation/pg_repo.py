# ruff: noqa: E501
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class PgConversationRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def create_session(self, channel: str) -> str:
        rows = await self._all("INSERT INTO conversation_sessions (channel) VALUES (%s) RETURNING id", (channel,))
        return str(rows[0]["id"])

    async def session_exists(self, session_id: str) -> bool:
        return bool(await self._all("SELECT 1 FROM conversation_sessions WHERE id = %s", (session_id,)))

    async def add_message(
        self, session_id: str, role: str, content: str, input_mode: str, model_profile_id: UUID | None
    ) -> str:
        rows = await self._all(
            "INSERT INTO conversation_messages (session_id, role, content, input_mode, model_profile_id) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (session_id, role, content, input_mode, model_profile_id),
        )
        return str(rows[0]["id"])

    async def update_message_content(self, message_id: str, content: str) -> None:
        await self._all(
            "UPDATE conversation_messages SET content = %s WHERE id = %s RETURNING id", (content, message_id)
        )

    async def recent_messages(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT role, content FROM (
                 SELECT role, content, created_at FROM conversation_messages
                 WHERE session_id = %s AND role IN ('user','assistant')
                 ORDER BY created_at DESC LIMIT %s) t
               ORDER BY created_at""",
            (session_id, limit),
        )

    async def add_voice_turn(
        self,
        session_id: str,
        transcript: str | None,
        interrupted: bool,
        stt_ms: int | None,
        rag_ms: int | None,
        llm_first_token_ms: int | None,
        tts_first_audio_ms: int | None,
    ) -> str:
        rows = await self._all(
            "INSERT INTO voice_turns (session_id, transcript, interrupted, stt_ms, rag_ms, llm_first_token_ms, "
            "tts_first_audio_ms) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (session_id, transcript, interrupted, stt_ms, rag_ms, llm_first_token_ms, tts_first_audio_ms),
        )
        return str(rows[0]["id"])

    _METRIC_COLS = (
        "session_id turn_id input_mode intent audience model_profile_id stt_ms rag_ms llm_first_token_ms "
        "tts_first_audio_ms total_ms prompt_tokens cached_tokens completion_tokens finish_reason sources_count "
        "top_source_score tool_calls tool_errors interrupted voseo_hits citations_removed final_text_rewritten"
    ).split()

    async def add_turn_metrics(self, row: dict[str, Any]) -> None:
        cols = [*self._METRIC_COLS, "case_state"]
        vals = [row.get(c) for c in self._METRIC_COLS] + [Jsonb(row.get("case_state") or {})]
        await self._all(
            f"INSERT INTO turn_metrics ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id", vals
        )

    async def mark_voice_turn_interrupted(self, voice_turn_id: str) -> None:
        await self._all("UPDATE voice_turns SET interrupted = true WHERE id = %s RETURNING id", (voice_turn_id,))

    _SESSION_SELECT = """SELECT s.id::text AS id, s.title, s.channel, s.status, s.archived, s.started_at, s.updated_at,
                      max(m.created_at) AS last_message_at, count(m.id)::int AS message_count
               FROM conversation_sessions s
               LEFT JOIN conversation_messages m ON m.session_id = s.id AND m.role IN ('user','assistant')"""

    async def touch_session(self, session_id: str, title_if_empty: str | None) -> None:
        await self._all(
            "UPDATE conversation_sessions SET updated_at = now(), title = COALESCE(title, %s) WHERE id = %s RETURNING id",
            (title_if_empty or None, session_id),
        )

    async def list_sessions(self, limit: int, include_archived: bool = False) -> list[dict[str, Any]]:
        return await self._all(
            self._SESSION_SELECT
            + " WHERE (%s OR NOT s.archived) GROUP BY s.id ORDER BY s.updated_at DESC, s.started_at DESC LIMIT %s",
            (include_archived, limit),
        )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        rows = await self._all(self._SESSION_SELECT + " WHERE s.id = %s GROUP BY s.id", (session_id,))
        return rows[0] if rows else None

    async def update_session(self, session_id: str, changes: dict[str, Any]) -> bool:
        sets, params = [], []
        for col in ("title", "archived"):  # whitelist: column names are never taken from the request
            if col in changes:
                sets.append(f"{col} = %s")
                params.append(changes[col])
        if not sets:
            return await self.session_exists(session_id)
        rows = await self._all(
            f"UPDATE conversation_sessions SET {', '.join(sets)} WHERE id = %s RETURNING id", (*params, session_id)
        )
        return bool(rows)

    async def delete_session(self, session_id: str) -> bool:
        # conversation_messages / voice_turns cascade; tool_audit_logs.session_id is SET NULL (audit kept)
        return bool(await self._all("DELETE FROM conversation_sessions WHERE id = %s RETURNING id", (session_id,)))

    async def list_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        try:
            UUID(session_id)
        except ValueError:
            return None
        if not await self.session_exists(session_id):
            return None
        return await self._all(
            "SELECT id::text AS id, role, content, input_mode, created_at FROM conversation_messages "
            "WHERE session_id = %s AND role <> 'system' ORDER BY created_at",
            (session_id,),
        )

    async def get_media(self, media_id: str, session_id: str) -> dict[str, Any] | None:
        """Row of `media` (table created by migration 013) owned by the session; None when absent or not migrated."""
        try:
            UUID(media_id)
        except ValueError:
            return None
        if not (await self._all("SELECT to_regclass('public.media') IS NOT NULL AS ok"))[0]["ok"]:
            return None
        rows = await self._all(
            "SELECT id::text AS id, session_id::text AS session_id, kind, mime, width, height, analysis, transcript "
            "FROM media WHERE id = %s AND session_id = %s",
            (media_id, session_id),
        )
        return rows[0] if rows else None
