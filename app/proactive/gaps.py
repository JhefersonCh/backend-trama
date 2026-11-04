from __future__ import annotations

import re
import unicodedata
from typing import Any

from psycopg.rows import dict_row

STOPWORDS = frozenset(
    "a al algo ante como con cual cuando de del el ella en es esta estan este esto la las lo los me mi mis muy no "
    "o para pero por porque que se si sin su sus te tengo tiene un una uno y ya yo hay son ser mas".split()
)
THRESHOLD = 0.5


def tokens(text: str) -> frozenset[str]:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return frozenset(w for w in t.split() if w not in STOPWORDS and len(w) > 1)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def group_gaps(rows: list[dict]) -> list[dict]:
    """rows: [{text, at}] -> clusters by greedy Jaccard >= 0.5 (rows ordered by time desc)."""
    clusters: list[dict] = []
    for r in rows:
        tk = tokens(r["text"])
        if not tk:
            continue
        for c in clusters:
            if _jaccard(tk, c["tokens"]) >= THRESHOLD:
                c["items"].append(r)
                break
        else:
            clusters.append({"tokens": tk, "items": [r]})
    out = []
    for c in clusters:
        items = c["items"]
        texts: list[str] = []
        for i in items:
            if i["text"] not in texts:
                texts.append(i["text"])
        out.append(
            {
                "representative_query": items[0]["text"],
                "count": len(items),
                "last_seen": max(i["at"] for i in items),
                "examples": texts[:3],
            }
        )
    return sorted(out, key=lambda x: (-x["count"], -x["last_seen"].timestamp()))


class PgGapsRepo:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def rows(self, iv: str, min_score: float) -> list[dict]:
        sql = """
            SELECT m.created_at AS at,
              (SELECT cm.content FROM conversation_messages cm
                WHERE cm.session_id = m.session_id AND cm.role = 'user' AND cm.created_at <= m.created_at
                ORDER BY cm.created_at DESC LIMIT 1) AS text
            FROM turn_metrics m
            WHERE m.created_at >= now() - %s::interval
              AND m.intent IN ('diagnostic','diagnostico','clarify','guide')
              AND (coalesce(m.sources_count, 0) = 0 OR m.top_source_score < %s)
            ORDER BY m.created_at DESC"""
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (iv, min_score))
            return [r for r in await cur.fetchall() if r["text"]]

    async def min_score(self) -> float:
        async with self._pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT value FROM app_settings WHERE key = 'rag_min_score'")
            r = await cur.fetchone()
        try:
            return float(r["value"]) if r else 0.35
        except (TypeError, ValueError):
            return 0.35
