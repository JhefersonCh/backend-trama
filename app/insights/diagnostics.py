# ruff: noqa: E501
"""Diagnóstico view: per-session case state, observations, sources, tools, tickets and timeline."""

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.requests import HTTPConnection
from psycopg.rows import dict_row

from app.config.errors import NotFoundError
from app.conversation import policy

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])
DIAG_INTENTS = (policy.DIAGNOSTIC, policy.GUIDE, policy.OUTPUT)
INACTIVE_AFTER = timedelta(hours=24)


class PgDiagnosticsRepo:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def list_sessions(self, limit: int) -> list[dict]:
        return await self._all(
            """SELECT s.id::text AS session_id, s.title, s.updated_at, l.intent AS last_intent, l.case_state,
                      l.created_at AS last_turn_at,
                      (SELECT t.ticket_number FROM tickets t JOIN incidents i ON i.id = t.incident_id
                        WHERE i.reporter_ref = s.id::text ORDER BY t.created_at DESC LIMIT 1) AS ticket_number,
                      (SELECT count(*)::int FROM diagnostic_observations o WHERE o.result->>'session_id' = s.id::text) AS observations
               FROM conversation_sessions s
               JOIN LATERAL (SELECT intent, case_state, created_at FROM turn_metrics m WHERE m.session_id = s.id
                             ORDER BY created_at DESC LIMIT 1) l ON true
               WHERE EXISTS (SELECT 1 FROM turn_metrics m WHERE m.session_id = s.id AND m.intent = ANY(%s))
               ORDER BY s.updated_at DESC LIMIT %s""",
            (list(DIAG_INTENTS), limit),
        )

    async def session(self, sid: str) -> dict | None:
        rows = await self._all(
            "SELECT id::text AS id, title, started_at, updated_at FROM conversation_sessions WHERE id = %s", (sid,)
        )
        return rows[0] if rows else None

    async def last_turn(self, sid: str) -> dict | None:
        rows = await self._all(
            "SELECT audience, case_state, created_at FROM turn_metrics WHERE session_id = %s ORDER BY created_at DESC LIMIT 1",
            (sid,),
        )
        return rows[0] if rows else None

    async def messages(self, sid: str) -> list[dict]:
        return await self._all(
            "SELECT role, content, created_at FROM conversation_messages WHERE session_id = %s AND role <> 'system' ORDER BY created_at",
            (sid,),
        )

    async def observations(self, sid: str) -> list[dict]:
        return await self._all(
            """SELECT observation_type, target, result - 'session_id' AS result, unit, source, collected_at
               FROM diagnostic_observations WHERE result->>'session_id' = %s ORDER BY collected_at""",
            (sid,),
        )

    async def tickets(self, sid: str) -> list[dict]:
        return await self._all(
            """SELECT t.ticket_number, t.status, t.priority, i.title, t.created_at
               FROM tickets t JOIN incidents i ON i.id = t.incident_id WHERE i.reporter_ref = %s ORDER BY t.created_at""",
            (sid,),
        )


def get_diag_repo(conn: HTTPConnection) -> PgDiagnosticsRepo:
    return PgDiagnosticsRepo(conn.app.state.pool)


Repo = Annotated[PgDiagnosticsRepo, Depends(get_diag_repo)]


def _clip(text: str, n: int = 140) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _status(has_ticket: bool, last_turn_at: datetime | None) -> str:
    if has_ticket:
        return "escalado"
    if last_turn_at is None or datetime.now(UTC) - last_turn_at > INACTIVE_AFTER:
        return "sin_actividad"
    return "en_curso"


def _summary(cs: dict | None) -> str:
    labels = [f["label"] for f in (cs or {}).get("facts", [])] + list((cs or {}).get("steps_tried", []))
    return " · ".join(x if i == 0 else x[:1].lower() + x[1:] for i, x in enumerate(labels[:3]))


def build_list(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        cs = r["case_state"] or {}
        out.append(
            {
                "session_id": r["session_id"],
                "title": r["title"],
                "updated_at": r["updated_at"].isoformat(),
                "last_intent": r["last_intent"],
                "status": _status(bool(r["ticket_number"]), r["last_turn_at"]),
                "ticket_number": r["ticket_number"],
                "observations": r["observations"],
                "facts": len(cs.get("facts", [])),
                "summary": _summary(cs) or r["title"] or "",
            }
        )
    return out


def _tool_rows(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] != "tool":
            continue
        try:
            d = json.loads(m["content"])
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append({**d, "at": m["created_at"]})
    return out


def build_detail(session: dict, last: dict | None, messages: list[dict], obs: list[dict], tickets: list[dict]) -> dict:
    user_texts = [m["content"] for m in messages if m["role"] == "user"]
    cs = (last or {}).get("case_state") or policy.case_snapshot(user_texts)
    audience = (last or {}).get("audience") or policy.audience("auto", user_texts)
    cs = {
        "facts": cs.get("facts", []),
        "steps_tried": cs.get("steps_tried", []),
        "ips": cs.get("ips", []),
        "devices": cs.get("devices"),
    }
    tool_rows = _tool_rows(messages)
    srcs: dict[tuple, dict] = {}
    for t in tool_rows:
        for s in t.get("sources") or []:
            e = srcs.setdefault(
                (s.get("title"), s.get("section")),
                {
                    "title": s.get("title"),
                    "section": s.get("section"),
                    "is_synthetic": bool(s.get("is_synthetic")),
                    "times": 0,
                    "best_score": 0.0,
                },
            )
            e["times"] += 1
            e["best_score"] = max(e["best_score"], s.get("score") or 0.0)
    timeline: list[dict] = []
    for m in messages:
        if m["role"] in ("user", "assistant"):
            c = m["content"]
            timeline.append(
                {
                    "at": m["created_at"],
                    "kind": m["role"],
                    "summary": _clip(c),
                    "detail": c if len(" ".join(c.split())) > 140 else None,
                }
            )
    for t in tool_rows:
        if t.get("sources"):
            titles = ", ".join(dict.fromkeys(s.get("title") or "?" for s in t["sources"]))
            timeline.append(
                {
                    "at": t["at"],
                    "kind": "sources",
                    "summary": _clip(f"{len(t['sources'])} fuente(s): {titles}"),
                    "detail": None,
                }
            )
        else:
            timeline.append(
                {
                    "at": t["at"],
                    "kind": "tool",
                    "summary": _clip(f"{t.get('name')}: {t.get('summary') or ''}"),
                    "detail": None,
                }
            )
    for o in obs:
        timeline.append(
            {
                "at": o["collected_at"],
                "kind": "tool",
                "summary": _clip(f"Observación {o['observation_type']} {o['target'] or ''}"),
                "detail": None,
            }
        )
    for t in tickets:
        timeline.append(
            {
                "at": t["created_at"],
                "kind": "ticket",
                "summary": _clip(f"Ticket {t['ticket_number']} creado: {t['title']}"),
                "detail": None,
            }
        )
    timeline.sort(key=lambda e: e["at"])
    last_at = (last or {}).get("created_at") or (messages[-1]["created_at"] if messages else None)
    return {
        "session": {
            "id": session["id"],
            "title": session["title"],
            "started_at": session["started_at"].isoformat(),
            "updated_at": session["updated_at"].isoformat(),
        },
        "status": _status(bool(tickets), last_at),
        "audience": audience,
        "case_state": cs,
        "observations": [{**o, "collected_at": o["collected_at"].isoformat()} for o in obs],
        "sources_used": sorted(
            ({**s, "best_score": round(s["best_score"], 3)} for s in srcs.values()), key=lambda s: -s["best_score"]
        ),
        "tools": [
            {"name": t.get("name"), "ok": bool(t.get("ok")), "summary": t.get("summary"), "at": t["at"].isoformat()}
            for t in tool_rows
            if not t.get("auto")
        ],
        "tickets": [{k: t[k] for k in ("ticket_number", "status", "priority", "title")} for t in tickets],
        "timeline": [{**e, "at": e["at"].isoformat()} for e in timeline],
    }


@router.get("")
async def list_diagnostics(repo: Repo, limit: Annotated[int, Query(ge=1, le=200)] = 30) -> list[dict]:
    return build_list(await repo.list_sessions(limit))


@router.get("/{session_id}")
async def diagnostic_detail(session_id: str, repo: Repo) -> dict:
    try:
        UUID(session_id)
    except ValueError:
        raise NotFoundError("session not found") from None
    if (session := await repo.session(session_id)) is None:
        raise NotFoundError("session not found")
    return build_detail(
        session,
        await repo.last_turn(session_id),
        await repo.messages(session_id),
        await repo.observations(session_id),
        await repo.tickets(session_id),
    )
