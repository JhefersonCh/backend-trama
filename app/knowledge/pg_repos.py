import re
from typing import Any
from uuid import UUID

import psycopg.errors as pge
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.config.errors import ConflictError

from .models import ChunkDraft, ChunkOut, Document, DocumentDetail, KnowledgeHit, ReindexJob
from .ports import PendingChunk

MAX_HNSW_DIM = 2000  # pgvector limit for HNSW on `vector`
_JOB_COLS = {"status", "progress", "error", "started_at", "finished_at"}
_DOC_SELECT = """
SELECT d.id, d.title, d.source_uri, d.publisher, d.version, d.license, d.language, d.checksum,
       d.category AS document_type, d.is_synthetic, d.data_origin, d.ingested_at,
       (SELECT count(*) FROM knowledge_chunks c WHERE c.document_id = d.id) AS chunk_count,
       (SELECT count(*) FROM knowledge_chunks c JOIN chunk_embeddings e ON e.chunk_id = c.id
         WHERE c.document_id = d.id AND e.embedding_profile_id = %s) AS embedded_count
FROM knowledge_documents d"""
_JOB_SELECT = (
    "SELECT id, from_profile_id, to_profile_id, status, progress::float AS progress, error, "
    "started_at, finished_at, created_at FROM reindex_jobs"
)


_STOP = frozenset(
    "el la los las un una unos unas de del al y o u en a por para con sin que se me mi mis tu tus su sus lo le les "
    "es son esta este esto eso ese pero como si no ya muy mas más hay tengo tiene tienen dice sale va ni cuando "
    "the a an of to is are in on and or for with my it this that".split()
)


def lexical_query(text: str) -> str:
    """OR-tsquery of the content words in a lay query ('' when nothing usable)."""
    toks = dict.fromkeys(t for t in re.findall(r"\w+(?:[.\-]\w+)*", text.lower()) if len(t) >= 3 and t not in _STOP)
    return " | ".join("'" + t.replace("'", "") + "'" for t in toks)


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in v) + "]"


def _uuid_lit(v: UUID) -> str:
    return "'" + str(UUID(str(v))) + "'"  # re-parsed UUID: safe to inline (DDL / index-matching predicates)


class PgKnowledgeRepo:
    _indexed: set[tuple[UUID, int]] = set()

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def _one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    async def _exec(self, sql: str, params: tuple | list = ()) -> int:
        async with self._pool.connection() as conn:
            return (await conn.execute(sql, params)).rowcount

    async def active_embedding_profile_id(self) -> UUID | None:
        r = await self._one("SELECT id FROM model_profiles WHERE role = 'embedding' AND is_active")
        return r["id"] if r else None

    async def find_by_checksum(self, checksum: str) -> UUID | None:
        r = await self._one("SELECT id FROM knowledge_documents WHERE checksum = %s", (checksum,))
        return r["id"] if r else None

    async def add_document(self, meta: dict[str, Any], chunks: list[ChunkDraft]) -> UUID:
        cols = list(meta)
        sql = (
            f"INSERT INTO knowledge_documents ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id"
        )
        try:
            async with self._pool.connection() as conn:  # one transaction
                cur = await conn.execute(sql, [meta[c] for c in cols])
                doc_id = (await cur.fetchone())[0]  # type: ignore[index]
                async with conn.cursor() as c2:
                    await c2.executemany(
                        "INSERT INTO knowledge_chunks (document_id, chunk_index, content, metadata) "
                        "VALUES (%s, %s, %s, %s)",
                        [(doc_id, c.chunk_index, c.content, Jsonb(c.metadata)) for c in chunks],
                    )
                return doc_id
        except pge.UniqueViolation as e:
            raise ConflictError("document already ingested (same checksum)") from e

    async def list_documents(self, profile_id: UUID | None) -> list[Document]:
        rows = await self._all(_DOC_SELECT + " ORDER BY d.ingested_at DESC", (profile_id,))
        return [Document(**r) for r in rows]

    async def get_document(self, id: UUID, profile_id: UUID | None) -> DocumentDetail | None:
        r = await self._one(_DOC_SELECT + " WHERE d.id = %s", (profile_id, id))
        if not r:
            return None
        chunks = await self._all(
            "SELECT id, chunk_index, content, metadata FROM knowledge_chunks "
            "WHERE document_id = %s ORDER BY chunk_index LIMIT 5",
            (id,),
        )
        meta = {
            k: v for k, v in (chunks[0]["metadata"] if chunks else {}).items() if k in ("publication_date", "chunking")
        }
        return DocumentDetail(**r, metadata=meta, preview=[ChunkOut(**c) for c in chunks])

    async def document_text(self, id: UUID) -> dict[str, Any] | None:
        r = await self._one(
            "SELECT id, title, source_uri, publisher, category AS document_type, language, license, is_synthetic, "
            "full_text FROM knowledge_documents WHERE id = %s",
            (id,),
        )
        if not r:
            return None
        r["chunks"] = await self._all(
            "SELECT chunk_index, metadata->>'section' AS section, content FROM knowledge_chunks "
            "WHERE document_id = %s ORDER BY chunk_index",
            (id,),
        )
        return r

    async def set_full_text(self, id: UUID, text: str) -> None:
        await self._exec(
            "UPDATE knowledge_documents SET full_text = %s WHERE id = %s AND full_text IS NULL", (text, id)
        )

    async def delete_document(self, id: UUID) -> bool:
        return await self._exec("DELETE FROM knowledge_documents WHERE id = %s", (id,)) > 0

    async def count_chunks(self) -> int:
        r = await self._one("SELECT count(*) AS n FROM knowledge_chunks")
        return r["n"] if r else 0

    async def count_embedded(self, profile_id: UUID) -> int:
        r = await self._one("SELECT count(*) AS n FROM chunk_embeddings WHERE embedding_profile_id = %s", (profile_id,))
        return r["n"] if r else 0

    _PENDING = (
        "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id "
        "WHERE NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id = c.id AND e.embedding_profile_id = %s)"
    )

    async def count_pending(self, profile_id: UUID, document_id: UUID | None = None) -> int:
        sql, args = "SELECT count(*) AS n " + self._PENDING, [profile_id]
        if document_id:
            sql, args = sql + " AND c.document_id = %s", [*args, document_id]
        r = await self._one(sql, args)
        return r["n"] if r else 0

    async def chunks_to_embed(
        self, profile_id: UUID, limit: int, document_id: UUID | None = None
    ) -> list[PendingChunk]:
        sql, args = "SELECT c.id, d.title, c.content " + self._PENDING, [profile_id]
        if document_id:
            sql, args = sql + " AND c.document_id = %s", [*args, document_id]
        rows = await self._all(sql + " ORDER BY c.document_id, c.chunk_index LIMIT %s", [*args, limit])
        return [PendingChunk(**r) for r in rows]

    async def save_embeddings(self, profile_id: UUID, items: list[tuple[UUID, list[float]]]) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO chunk_embeddings (chunk_id, embedding_profile_id, dimension, embedding) "
                "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (chunk_id, embedding_profile_id) DO NOTHING",
                [(cid, profile_id, len(v), _vec(v)) for cid, v in items],
            )

    async def ensure_index(self, profile_id: UUID, dim: int) -> None:
        key = (profile_id, int(dim))
        if key in self._indexed or not 0 < int(dim) <= MAX_HNSW_DIM:
            return
        name = f"idx_chunk_emb_hnsw_{profile_id.hex}"
        sql = (
            f"CREATE INDEX IF NOT EXISTS {name} ON chunk_embeddings "
            f"USING hnsw ((embedding::vector({int(dim)})) vector_cosine_ops) "
            f"WHERE embedding_profile_id = {_uuid_lit(profile_id)}"
        )
        await self._exec(sql)
        self._indexed.add(key)

    async def rank_settings(self) -> dict[str, Any]:
        rows = await self._all(
            "SELECT key, value FROM app_settings WHERE key IN ('rag_type_weights', 'rag_lexical_weight')"
        )
        by = {r["key"]: r["value"] for r in rows}
        w, lex = by.get("rag_type_weights"), by.get("rag_lexical_weight")
        return {
            "type_weights": w if isinstance(w, dict) else {},
            "lexical_weight": float(lex) if isinstance(lex, int | float) and not isinstance(lex, bool) else 0.0,
        }

    async def search(
        self,
        profile_id: UUID,
        dim: int,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any],
        rank: dict[str, Any] | None = None,
    ) -> list[KnowledgeHit]:
        await self.ensure_index(profile_id, dim)
        d = int(dim)
        where, fargs = [f"e.embedding_profile_id = {_uuid_lit(profile_id)}"], []
        col = {"document_type": "d.category", "language": "d.language", "is_synthetic": "d.is_synthetic"}
        for k, v in filters.items():
            where.append(f"{col[k]} = %s")
            fargs.append(v)
        tsq = lexical_query(rank["query"]) if rank and rank.get("lexical_weight") else ""
        lex_sql = "ts_rank_cd(to_tsvector('simple', c.content), to_tsquery('simple', %s), 32)" if tsq else "0::float"
        limit = max(top_k * 8, 50) if rank else top_k  # hybrid: re-rank a dense candidate pool
        sql = f"""
SELECT c.id::text AS chunk_id, c.chunk_index, c.document_id::text AS document_id, d.title, c.content, d.source_uri,
       c.metadata->>'section' AS section, d.is_synthetic, d.category AS document_type,
       1 - ((e.embedding::vector({d})) <=> %s::vector({d})) AS score, {lex_sql} AS lex
FROM chunk_embeddings e
JOIN knowledge_chunks c ON c.id = e.chunk_id
JOIN knowledge_documents d ON d.id = c.document_id
WHERE {" AND ".join(where)}
ORDER BY (e.embedding::vector({d})) <=> %s::vector({d})
LIMIT %s"""
        # placeholder order: score vec, [tsquery], filters..., order vec, limit
        params = [_vec(vector), *([tsq] if tsq else []), *fargs, _vec(vector), limit]
        rows = await self._all(sql, params)
        if not rank:
            return [KnowledgeHit(**{k: v for k, v in r.items() if k not in ("lex", "document_type")}) for r in rows]
        weights, lw = rank.get("type_weights") or {}, float(rank.get("lexical_weight") or 0)
        for r in rows:
            r["rank_score"] = r["score"] * float(weights.get(r["document_type"], 1.0)) + lw * float(r["lex"] or 0)
        rows.sort(key=lambda r: r["rank_score"], reverse=True)
        return [KnowledgeHit(**{k: v for k, v in r.items() if k not in ("lex", "document_type")}) for r in rows[:top_k]]

    # ------------------------------------------------------------ jobs
    async def create_job(self, to_profile_id: UUID) -> ReindexJob:
        r = await self._one("INSERT INTO reindex_jobs (to_profile_id) VALUES (%s) RETURNING id", (to_profile_id,))
        return await self.get_job(r["id"])  # type: ignore[index,return-value]

    async def update_job(self, id: UUID, **fields: Any) -> ReindexJob:
        bad = set(fields) - _JOB_COLS
        if bad:
            raise ValueError(f"invalid job fields: {bad}")
        sets = ", ".join(f"{k} = %s" for k in fields)
        await self._exec(f"UPDATE reindex_jobs SET {sets} WHERE id = %s", [*fields.values(), id])
        return await self.get_job(id)  # type: ignore[return-value]

    async def get_job(self, id: UUID) -> ReindexJob | None:
        r = await self._one(_JOB_SELECT + " WHERE id = %s", (id,))
        return ReindexJob(**r) if r else None

    async def latest_job(self) -> ReindexJob | None:
        r = await self._one(_JOB_SELECT + " ORDER BY created_at DESC LIMIT 1")
        return ReindexJob(**r) if r else None

    async def active_job(self) -> ReindexJob | None:
        r = await self._one(
            _JOB_SELECT + " WHERE status IN ('pending','running') AND created_at > now() - interval '1 hour' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        return ReindexJob(**r) if r else None
