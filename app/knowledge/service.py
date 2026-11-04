import asyncio
import hashlib
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config.errors import ConflictError, DomainValidationError, NotFoundError

from .chunking import (
    CHUNKING_PARAMS,
    build_chunks,
    full_text_from_parsed,
    locate_chunks,
    parse_file,
    rebuild_full_text,
)
from .errors import ReindexRequiredError
from .models import (
    ContentSection,
    DocumentContent,
    DocumentDetail,
    DocumentMeta,
    IngestResult,
    KnowledgeHit,
    ReindexJob,
)
from .ports import KnowledgeRepo, PendingChunk

log = logging.getLogger("trama.knowledge")

BATCH = 16
DEFAULT_DOC_PREFIX = "title: {title} | text: {content}"
DEFAULT_QUERY_PREFIX = "task: search result | query: {query}"
FILTERS = {"document_type", "language", "is_synthetic"}
_tasks: set[asyncio.Task] = set()  # strong refs so background reindex jobs are not GC'd


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def _fill(template: str, **vals: str) -> str:
    for k, v in vals.items():
        template = template.replace("{" + k + "}", v)
    return template


_SHRINK = (900, 600, 350)  # chars; symbol-dense text can exceed the runtime's per-input token limit


async def _embed_resilient(adapter: Any, profile: Any, texts: list[str]) -> list[list[float]]:
    """Embed `texts`; if the runtime rejects the batch, bisect it, and shrink a single input as a last resort
    (only the embedded text is shortened; the stored chunk is untouched)."""
    try:
        return await adapter.embed(profile.model_ref, texts, profile.params)
    except Exception as first:
        if len(texts) > 1:
            mid = len(texts) // 2
            return [
                *await _embed_resilient(adapter, profile, texts[:mid]),
                *await _embed_resilient(adapter, profile, texts[mid:]),
            ]
        for limit in _SHRINK:
            try:
                return await adapter.embed(profile.model_ref, [texts[0][:limit]], profile.params)
            except Exception:  # noqa: S112 - keep shrinking
                continue
        raise first


class KnowledgeService:
    """Ingestion, embedding and reindex. `manager` only needs `.use(role)` (ModelManager)."""

    def __init__(self, repo: KnowledgeRepo, manager: Any) -> None:
        self._repo = repo
        self._manager = manager

    # ------------------------------------------------------------ ingest
    async def ingest(self, filename: str, data: bytes, meta: DocumentMeta) -> IngestResult:
        checksum = hashlib.sha256(data).hexdigest()
        if await self._repo.find_by_checksum(checksum):
            raise ConflictError("document already ingested (same checksum)")
        parsed = parse_file(filename, data)
        if not parsed.sections:
            raise DomainValidationError("file has no text content")
        title = meta.title or parsed.title or filename.rsplit(".", 1)[0]
        base = {
            "document_type": meta.document_type,
            "publication_date": meta.publication_date,
            "chunking": CHUNKING_PARAMS,
            "filename": filename,
        }
        chunks = build_chunks(parsed, base)
        doc_id = await self._repo.add_document(
            {
                "title": title,
                "source_uri": meta.source_uri,
                "publisher": meta.publisher,
                "version": meta.version,
                "license": meta.license,
                "language": meta.language,
                "checksum": checksum,
                "category": meta.document_type,
                "is_synthetic": meta.is_synthetic or meta.data_origin == "synthetic",
                "data_origin": meta.data_origin,
                "full_text": full_text_from_parsed(parsed),
            },
            chunks,
        )
        warning = None
        try:
            await self._embed_pending(document_id=doc_id)
        except Exception as e:  # runtime down / no profile: document stays, reindex later
            log.warning("embedding after ingest failed: %s", e)
            warning = f"document stored but not embedded ({type(e).__name__}: {e}); run POST /api/knowledge/reindex"
        profile_id = await self._repo.active_embedding_profile_id()
        doc = await self._repo.get_document(doc_id, profile_id)
        assert doc is not None
        return IngestResult(document=doc, chunks=doc.chunk_count, embedded=doc.embedded_count, warning=warning)

    async def list_documents(self):
        return await self._repo.list_documents(await self._repo.active_embedding_profile_id())

    async def get_document(self, id: UUID) -> DocumentDetail:
        doc = await self._repo.get_document(id, await self._repo.active_embedding_profile_id())
        if doc is None:
            raise NotFoundError("document not found")
        return doc

    async def get_content(self, id: UUID) -> DocumentContent:
        d = await self._repo.document_text(id)
        if d is None:
            raise NotFoundError("document not found")
        chunks = d.pop("chunks")
        text = d.pop("full_text", None)
        if not text:  # ingested before migration 011: rebuild from chunks and keep it
            text = rebuild_full_text(chunks)
            if text:
                await self._repo.set_full_text(id, text)
        spans = locate_chunks(text, chunks)
        sections = [
            ContentSection(chunk_index=c["chunk_index"], section=c.get("section"), start=s, end=e)
            for c, (s, e) in zip(chunks, spans, strict=True)
        ]
        return DocumentContent(**d, full_text=text, sections=sections)

    async def delete_document(self, id: UUID) -> None:
        if not await self._repo.delete_document(id):
            raise NotFoundError("document not found")

    # ------------------------------------------------------------ embedding
    async def _embed_pending(
        self,
        document_id: UUID | None = None,
        on_progress: Callable[[float], Awaitable[None]] | None = None,
    ) -> int:
        done = 0
        async with self._manager.use("embedding") as lease:
            profile = lease.profile
            prefix = profile.params.get("doc_prefix") or DEFAULT_DOC_PREFIX
            total = await self._repo.count_pending(profile.id, document_id)
            indexed = False
            while True:
                batch: list[PendingChunk] = await self._repo.chunks_to_embed(profile.id, BATCH, document_id)
                if not batch:
                    break
                texts = [_fill(prefix, title=c.title, content=c.content) for c in batch]
                vecs = await _embed_resilient(lease.adapter, profile, texts)
                if len(vecs) != len(batch):
                    raise RuntimeError("embedding runtime returned a wrong number of vectors")
                await self._repo.save_embeddings(
                    profile.id, [(c.id, _normalize(v)) for c, v in zip(batch, vecs, strict=False)]
                )
                if not indexed:
                    await self._repo.ensure_index(profile.id, len(vecs[0]))
                    indexed = True
                done += len(batch)
                if on_progress and total:
                    await on_progress(min(100.0, round(done * 100 / total, 2)))
        return done

    # ------------------------------------------------------------ reindex
    async def start_reindex(self) -> ReindexJob:
        profile_id = await self._repo.active_embedding_profile_id()
        if profile_id is None:
            raise ConflictError("no active embedding profile")
        if await self._repo.active_job():
            raise ConflictError("a reindex job is already running")
        job = await self._repo.create_job(profile_id)
        task = asyncio.create_task(self._run_job(job.id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return job

    async def _run_job(self, job_id: UUID) -> None:
        repo = self._repo
        await repo.update_job(job_id, status="running", started_at=datetime.now(UTC))

        async def progress(p: float) -> None:
            await repo.update_job(job_id, progress=p)

        try:
            await self._embed_pending(on_progress=progress)
            await repo.update_job(job_id, status="completed", progress=100, finished_at=datetime.now(UTC))
        except Exception as e:
            log.warning("reindex job %s failed: %s", job_id, e)
            await repo.update_job(job_id, status="failed", error=str(e)[:500], finished_at=datetime.now(UTC))

    async def get_job(self, id: UUID) -> ReindexJob:
        job = await self._repo.get_job(id)
        if job is None:
            raise NotFoundError("reindex job not found")
        return job

    async def latest_job(self) -> ReindexJob:
        job = await self._repo.latest_job()
        if job is None:
            raise NotFoundError("no reindex jobs yet")
        return job


class KnowledgeSearchService:
    """Semantic search against the chunks embedded with the ACTIVE embedding profile."""

    def __init__(self, repo: KnowledgeRepo, manager: Any) -> None:
        self._repo = repo
        self._manager = manager

    async def search(
        self,
        query: str,
        top_k: int,
        filters: dict | None = None,
        on_load: Callable[[], Awaitable[None]] | None = None,
        lexical_weight: float | None = None,
        type_weights: dict[str, float] | None = None,
    ) -> list[KnowledgeHit]:
        """`on_load` is awaited only if the embedding model actually has to be loaded first."""
        clean = {k: v for k, v in (filters or {}).items() if k in FILTERS and v is not None}
        async with self._manager.use("embedding", on_load=on_load) as lease:
            profile = lease.profile
            if not await self._repo.count_embedded(profile.id):
                if await self._repo.count_chunks():
                    raise ReindexRequiredError("reindex required: no chunks embedded for the active embedding profile")
                return []
            prefix = profile.params.get("query_prefix") or DEFAULT_QUERY_PREFIX
            vecs = await lease.adapter.embed(profile.model_ref, [_fill(prefix, query=query)], profile.params)
        vec = _normalize(vecs[0])
        cfg = await self._repo.rank_settings()
        lex = cfg.get("lexical_weight", 0.0) if lexical_weight is None else lexical_weight
        weights = cfg.get("type_weights") or {} if type_weights is None else type_weights
        rank = (
            {"query": query, "lexical_weight": float(lex or 0), "type_weights": weights} if (lex or weights) else None
        )
        return await self._repo.search(profile.id, len(vec), vec, top_k, clean, rank)
