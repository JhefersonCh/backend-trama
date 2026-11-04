# ruff: noqa: E501
"""Métricas view: SQL aggregates over turn_metrics / tool_audit_logs / model_load_events / tickets."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.requests import HTTPConnection
from psycopg.rows import dict_row

router = APIRouter(prefix="/api/metrics", tags=["metrics"])
RANGES = {"24h": "24 hours", "7d": "7 days", "30d": "30 days"}
Range = Literal["24h", "7d", "30d"]
_PCT = ("stt_ms", "rag_ms", "llm_first_token_ms", "tts_first_audio_ms", "total_ms")


class PgMetricsRepo:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: dict | tuple = ()) -> list[dict]:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def totals(self, iv: str) -> dict:
        pct = ", ".join(
            f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {c}) AS {c}_p50, "
            f"percentile_cont(0.95) WITHIN GROUP (ORDER BY {c}) AS {c}_p95"
            for c in _PCT
        )
        rows = await self._all(
            f"""SELECT count(*)::int AS total, count(*) FILTER (WHERE interrupted)::int AS interrupted, {pct},
                avg(prompt_tokens) AS prompt_avg, avg(cached_tokens) AS cached_avg, avg(completion_tokens) AS completion_avg,
                sum(cached_tokens) AS cached_sum, sum(prompt_tokens) AS prompt_sum,
                count(*) FILTER (WHERE sources_count > 0)::int AS with_sources,
                count(*) FILTER (WHERE rag_ms IS NOT NULL AND coalesce(sources_count, 0) = 0)::int AS no_context,
                avg(top_source_score) FILTER (WHERE sources_count > 0) AS avg_top_score,
                coalesce(sum(voseo_hits), 0)::int AS voseo, coalesce(sum(citations_removed), 0)::int AS cites,
                count(*) FILTER (WHERE final_text_rewritten)::int AS rewrites,
                count(*) FILTER (WHERE finish_reason = 'length')::int AS cutoffs
                FROM turn_metrics WHERE created_at >= now() - %s::interval""",
            (iv,),
        )
        return rows[0]

    async def grouped(self, col: str, iv: str) -> list[dict]:
        assert col in ("intent", "input_mode")
        return await self._all(
            f"SELECT {col} AS k, count(*)::int AS n FROM turn_metrics WHERE created_at >= now() - %s::interval GROUP BY 1",
            (iv,),
        )

    async def tools(self, iv: str) -> list[dict]:
        return await self._all(
            """SELECT tool_name AS name, count(*)::int AS calls, count(*) FILTER (WHERE status = 'ok')::int AS ok,
                      count(*) FILTER (WHERE status <> 'ok')::int AS errors
               FROM tool_audit_logs WHERE executed_at >= now() - %s::interval GROUP BY 1 ORDER BY 2 DESC""",
            (iv,),
        )

    async def model_loads(self, iv: str) -> list[dict]:
        return await self._all(
            """SELECT p.name AS profile, p.role, count(*) FILTER (WHERE e.event = 'load')::int AS loads,
                      count(*) FILTER (WHERE e.event = 'evict')::int AS evictions,
                      count(*) FILTER (WHERE e.event = 'error')::int AS errors,
                      avg(e.duration_ms) FILTER (WHERE e.event = 'load') AS avg_ms
               FROM model_load_events e JOIN model_profiles p ON p.id = e.profile_id
               WHERE e.created_at >= now() - %s::interval GROUP BY p.name, p.role ORDER BY p.name""",
            (iv,),
        )

    async def tickets(self, iv: str) -> dict:
        return (
            await self._all(
                """SELECT count(*) FILTER (WHERE status = 'open')::int AS open,
                          count(*) FILTER (WHERE status IN ('assigned','in_progress'))::int AS in_progress,
                          count(*) FILTER (WHERE status = 'resolved')::int AS resolved,
                          count(*) FILTER (WHERE status = 'closed')::int AS closed,
                          count(*) FILTER (WHERE created_at >= now() - %s::interval)::int AS created_in_range
                   FROM tickets""",
                (iv,),
            )
        )[0]

    async def sessions(self, iv: str) -> int:
        r = await self._all(
            "SELECT count(*)::int AS n FROM conversation_sessions WHERE started_at >= now() - %s::interval", (iv,)
        )
        return r[0]["n"]

    async def timeseries(self, unit: str, iv: str) -> list[dict]:
        step = "1 hour" if unit == "hour" else "1 day"
        return await self._all(
            """SELECT b.t, count(m.id)::int AS turns,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY m.total_ms) AS total_p50,
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY m.total_ms) AS total_p95,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY m.llm_first_token_ms) AS llm_first_token_p50
               FROM generate_series(date_trunc(%(u)s, now() - %(iv)s::interval), date_trunc(%(u)s, now()), %(step)s::interval) AS b(t)
               LEFT JOIN turn_metrics m ON date_trunc(%(u)s, m.created_at) = b.t
               GROUP BY b.t ORDER BY b.t""",
            {"u": unit, "iv": iv, "step": step},
        )

    async def recent_turns(self, limit: int) -> list[dict]:
        return await self._all(
            """SELECT m.created_at, m.session_id::text AS session_id, s.title AS session_title, m.intent, m.input_mode,
                      p.name AS model, m.total_ms, m.llm_first_token_ms, m.rag_ms, m.sources_count, m.tool_calls,
                      m.finish_reason, m.interrupted, m.voseo_hits
               FROM turn_metrics m JOIN conversation_sessions s ON s.id = m.session_id
               LEFT JOIN model_profiles p ON p.id = m.model_profile_id
               ORDER BY m.created_at DESC LIMIT %s""",
            (limit,),
        )


def get_metrics_repo(conn: HTTPConnection) -> PgMetricsRepo:
    return PgMetricsRepo(conn.app.state.pool)


Repo = Annotated[PgMetricsRepo, Depends(get_metrics_repo)]


def _n(v: Any) -> int | None:
    return None if v is None else round(float(v))


def _pair(t: dict, c: str) -> dict:
    return {"p50": _n(t[f"{c}_p50"]), "p95": _n(t[f"{c}_p95"])}


def build_summary(
    rng: str,
    t: dict,
    intents: list[dict],
    modes: list[dict],
    tools: list[dict],
    loads: list[dict],
    tickets: dict,
    sessions: int,
) -> dict:
    mode = {r["k"]: r["n"] for r in modes}
    prompt_sum = t["prompt_sum"] or 0
    return {
        "range": rng,
        "turns": {
            "total": t["total"],
            "by_intent": {r["k"] or "unknown": r["n"] for r in intents},
            "by_mode": {"text": mode.get("text", 0), "voice": mode.get("voice", 0)},
            "interrupted": t["interrupted"],
        },
        "latency_ms": {
            "stt": _pair(t, "stt_ms"),
            "rag": _pair(t, "rag_ms"),
            "llm_first_token": _pair(t, "llm_first_token_ms"),
            "tts_first_audio": _pair(t, "tts_first_audio_ms"),
            "total": _pair(t, "total_ms"),
        },
        "tokens": {
            "prompt_avg": _n(t["prompt_avg"]) or 0,
            "cached_avg": _n(t["cached_avg"]) or 0,
            "completion_avg": _n(t["completion_avg"]) or 0,
            "cache_hit_ratio": round((t["cached_sum"] or 0) / prompt_sum, 4) if prompt_sum else 0.0,
        },
        "rag": {
            "turns_with_sources": t["with_sources"],
            "no_context_turns": t["no_context"],
            "avg_top_score": None if t["avg_top_score"] is None else round(float(t["avg_top_score"]), 3),
        },
        "quality": {
            "voseo_hits": t["voseo"],
            "citations_removed": t["cites"],
            "final_text_rewrites": t["rewrites"],
            "length_cutoffs": t["cutoffs"],
        },
        "tools": [{**r, "avg_ms": None} for r in tools],  # tool_audit_logs has no duration column
        "model_loads": [{**r, "avg_ms": _n(r["avg_ms"])} for r in loads],
        "tickets": {k: tickets[k] for k in ("open", "in_progress", "resolved", "closed", "created_in_range")},
        "sessions": {"in_range": sessions},
    }


@router.get("/summary")
async def summary(repo: Repo, range: Range = "7d") -> dict:  # noqa: A002
    iv = RANGES[range]
    return build_summary(
        range,
        await repo.totals(iv),
        await repo.grouped("intent", iv),
        await repo.grouped("input_mode", iv),
        await repo.tools(iv),
        await repo.model_loads(iv),
        await repo.tickets(iv),
        await repo.sessions(iv),
    )


@router.get("/timeseries")
async def timeseries(repo: Repo, range: Range = "7d") -> dict:  # noqa: A002
    unit = "hour" if range == "24h" else "day"
    rows = await repo.timeseries(unit, RANGES[range])
    return {
        "bucket": unit,
        "points": [
            {
                "t": r["t"].isoformat(),
                "turns": r["turns"],
                "total_p50": _n(r["total_p50"]),
                "total_p95": _n(r["total_p95"]),
                "llm_first_token_p50": _n(r["llm_first_token_p50"]),
            }
            for r in rows
        ],
    }


@router.get("/turns")
async def turns(repo: Repo, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    return [{**r, "created_at": r["created_at"].isoformat()} for r in await repo.recent_turns(limit)]
