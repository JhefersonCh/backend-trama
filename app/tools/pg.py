# ruff: noqa: E501
"""Postgres implementations of the tool ports. Read-only parameterized SQL; no free SQL from the LLM."""

import asyncio
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class PgToolAudit:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def record(
        self,
        session_id: str | None,
        tool_name: str,
        arguments_summary: str,
        result_summary: str,
        status: str,
        *,
        tool_version: int | None = None,
        duration_ms: int | None = None,
        authorized_by: str | None = None,
    ) -> None:
        async with self._pool.connection() as c:
            await c.execute(
                "INSERT INTO tool_audit_logs (session_id, tool_name, arguments_summary, result_summary, status, "
                "tool_version, duration_ms, authorized_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    session_id,
                    tool_name,
                    arguments_summary,
                    result_summary,
                    status,
                    tool_version,
                    duration_ms,
                    authorized_by,
                ),
            )


class PgInventory:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def inventory(self, site: str | None) -> dict[str, Any]:
        pat = _like(site) if site else None
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT s.name AS site, d.name, d.device_type, d.vendor, d.model,
                          host(d.management_ip) AS management_ip, d.status, d.data_origin,
                          (SELECT count(*) FROM interfaces i WHERE i.device_id = d.id)::int AS interfaces
                   FROM network_devices d JOIN sites s ON s.id = d.site_id
                   WHERE (%(pat)s::text IS NULL OR s.name ILIKE %(pat)s)
                   ORDER BY s.name, d.name LIMIT 200""",
                {"pat": pat},
            )
            devices = await cur.fetchall()
            await cur.execute(
                """SELECT s.name AS site, n.name, n.cidr::text AS cidr, host(n.gateway) AS gateway, n.vlan_id,
                          ARRAY(SELECT host(x) FROM unnest(n.dns_servers) x) AS dns_servers, n.data_origin
                   FROM networks n JOIN sites s ON s.id = n.site_id
                   WHERE (%(pat)s::text IS NULL OR s.name ILIKE %(pat)s)
                   ORDER BY s.name, n.name LIMIT 200""",
                {"pat": pat},
            )
            networks = await cur.fetchall()
        return {"devices": devices, "networks": networks}

    async def network_details(self, name_or_cidr: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT n.id, n.site_id, s.name AS site, n.name, n.cidr::text AS cidr, host(n.gateway) AS gateway,
                          n.vlan_id, ARRAY(SELECT host(x) FROM unnest(n.dns_servers) x) AS dns_servers, n.data_origin
                   FROM networks n JOIN sites s ON s.id = n.site_id
                   WHERE n.name ILIKE %(pat)s OR n.cidr::text = %(q)s
                   ORDER BY s.name, n.name LIMIT 10""",
                {"pat": _like(name_or_cidr), "q": name_or_cidr},
            )
            nets = await cur.fetchall()
            for n in nets:
                await cur.execute(
                    """SELECT d.name AS device, i.name AS interface, host(i.ip_address) AS ip_address,
                              i.vlan_id, i.admin_status, i.oper_status
                       FROM interfaces i JOIN network_devices d ON d.id = i.device_id
                       WHERE d.site_id = %s AND i.ip_address <<= %s::cidr
                       ORDER BY d.name, i.name LIMIT 100""",
                    (n["site_id"], n["cidr"]),
                )
                n["interfaces"] = await cur.fetchall()
                del n["id"], n["site_id"]
        return nets


class PgDiagnostics:
    """One diagnostic_run per session, created lazily. (diagnostic_runs has no session column: kept in memory +
    stamped in each observation's result.)"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._runs: dict[str | None, Any] = {}
        self._lock = asyncio.Lock()

    async def record(self, session_id: str | None, observations: list[dict[str, Any]]) -> int:
        if not observations:
            return 0
        async with self._pool.connection() as c:
            async with self._lock:
                run_id = self._runs.get(session_id)
                if run_id is None:
                    cur = await c.execute("INSERT INTO diagnostic_runs DEFAULT VALUES RETURNING id")
                    run_id = (await cur.fetchone())[0]  # type: ignore[index]
                    self._runs[session_id] = run_id
            for o in observations:
                await c.execute(
                    "INSERT INTO diagnostic_observations (diagnostic_run_id, observation_type, target, result, unit, source) "
                    "VALUES (%s,%s,%s,%s,%s,'user_provided')",
                    (
                        run_id,
                        o["observation_type"],
                        o.get("target"),
                        Jsonb({**o["result"], "session_id": session_id}),
                        o.get("unit"),
                    ),
                )
        return len(observations)
