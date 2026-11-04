# ruff: noqa: E501
"""Postgres repository for agent_tools (+ versions, usage from tool_audit_logs, read-only SELECT runner)."""

from typing import Any
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.config.errors import ConflictError, NotFoundError

from .sqlguard import MAX_ROWS

_COLS = "id, name, kind, description, params_schema, config, intents, enabled, version, updated_at"
_SELECT = """
SELECT t.id, t.name, t.kind, t.description, t.params_schema, t.config, t.intents, t.enabled, t.version, t.updated_at,
       COALESCE(u.calls, 0)::int AS calls, COALESCE(u.errors, 0)::int AS errors, u.last_used_at
FROM agent_tools t
LEFT JOIN (
  SELECT tool_name, count(*) AS calls, count(*) FILTER (WHERE status <> 'ok') AS errors, max(executed_at) AS last_used_at
  FROM tool_audit_logs WHERE authorized_by IS DISTINCT FROM 'ui_test' GROUP BY tool_name
) u ON u.tool_name = t.name"""


def _shape(r: dict[str, Any]) -> dict[str, Any]:
    r = dict(r)
    r["usage"] = {"calls": r.pop("calls", 0), "errors": r.pop("errors", 0), "last_used_at": r.pop("last_used_at", None)}
    return r


class PgToolsRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT + " ORDER BY t.name")
            return [_shape(r) for r in await cur.fetchall()]

    async def get(self, id: UUID) -> dict[str, Any]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT + " WHERE t.id = %s", (id,))
            r = await cur.fetchone()
        if r is None:
            raise NotFoundError("herramienta no encontrada")
        return _shape(r)

    async def insert(self, d: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"INSERT INTO agent_tools (name, kind, description, params_schema, config, intents, enabled) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
                    (d["name"], d["kind"], d["description"], Jsonb(d["params_schema"]), Jsonb(d["config"]),
                     d["intents"], d["enabled"]),
                )  # fmt: skip
                row = await cur.fetchone()
                await self._snapshot(cur, row)
        except UniqueViolation:
            raise ConflictError(f"ya existe una herramienta llamada {d['name']!r}") from None
        return _shape(row)  # type: ignore[arg-type]

    async def update(self, id: UUID, d: dict[str, Any]) -> dict[str, Any]:
        """Applies the changed fields, bumps `version` and stores a snapshot."""
        sets, vals = [], []
        for k in ("description", "params_schema", "config", "intents", "enabled"):
            if k in d:
                sets.append(f"{k} = %s")
                vals.append(Jsonb(d[k]) if k in ("params_schema", "config") else d[k])
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE agent_tools SET {', '.join(sets)}, version = version + 1, updated_at = now() "
                f"WHERE id = %s RETURNING {_COLS}",
                (*vals, id),
            )
            row = await cur.fetchone()
            if row is None:
                raise NotFoundError("herramienta no encontrada")
            await self._snapshot(cur, row)
        return await self.get(id)

    async def set_enabled(self, id: UUID, enabled: bool) -> dict[str, Any]:
        async with self._pool.connection() as c:
            cur = await c.execute(
                "UPDATE agent_tools SET enabled = %s, updated_at = now() WHERE id = %s", (enabled, id)
            )
            if cur.rowcount == 0:
                raise NotFoundError("herramienta no encontrada")
        return await self.get(id)

    async def _snapshot(self, cur: Any, row: dict[str, Any]) -> None:
        snap = {k: (str(v) if k in ("id", "updated_at") else v) for k, v in row.items()}
        await cur.execute(
            "INSERT INTO agent_tool_versions (tool_id, version, snapshot) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (row["id"], row["version"], Jsonb(snap)),
        )

    async def versions(self, id: UUID) -> list[dict[str, Any]]:
        await self.get(id)
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT version, snapshot, created_at FROM agent_tool_versions WHERE tool_id = %s "
                "ORDER BY version DESC LIMIT 50",
                (id,),
            )
            return await cur.fetchall()

    async def run_select(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Runs an already validated SELECT as role trama_tools_ro: read-only transaction, 2 s timeout, 50 rows."""
        async with self._pool.connection() as c:
            async with c.transaction():
                await c.execute("SET TRANSACTION READ ONLY")
                await c.execute("SET LOCAL ROLE trama_tools_ro")
                await c.execute("SET LOCAL statement_timeout = '2s'")
                async with c.cursor(row_factory=dict_row) as cur:
                    await cur.execute(sql, params)
                    return await cur.fetchmany(MAX_ROWS)

    async def secret_ciphertext(self, ref: str) -> bytes | None:
        """`provider:<name or uuid>` -> encrypted secret of that provider (decrypted by the caller, never logged)."""
        kind, _, ident = ref.partition(":")
        if kind != "provider" or not ident:
            return None
        async with self._pool.connection() as c, c.cursor() as cur:
            await cur.execute(
                "SELECT s.ciphertext FROM provider_secrets s JOIN model_providers p ON p.id = s.provider_id "
                "WHERE p.name = %s OR p.id::text = %s",
                (ident, ident),
            )
            r = await cur.fetchone()
        return bytes(r[0]) if r else None
