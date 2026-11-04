"""In-memory repos + deterministic embedding runtime for knowledge/tickets endpoint tests."""

import math
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.config.errors import ConflictError
from app.knowledge.models import ChunkOut, Document, DocumentDetail, KnowledgeHit, ReindexJob
from app.knowledge.ports import PendingChunk

DIM = 32


def _now():
    return datetime.now(UTC)


def bow_vector(text: str) -> list[float]:
    """Bag-of-words hashed into DIM buckets: similar words -> similar vectors, fully deterministic."""
    v = [0.0] * DIM
    for w in re.findall(r"\w+", text.lower()):
        v[sum(map(ord, w)) % DIM] += 1.0
    return v


class FakeAdapter:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed(self, model, texts, params=None):
        self.calls.append(list(texts))
        return [bow_vector(t) for t in texts]


class FakeEmbedManager:
    """Mimics ModelManager.use('embedding'); `switch_profile()` simulates activating another profile."""

    def __init__(self):
        self.adapter = FakeAdapter()
        self.profile = SimpleNamespace(id=uuid4(), model_ref="emb", params={})
        self.down = False

    def switch_profile(self):
        self.profile = SimpleNamespace(id=uuid4(), model_ref="emb2", params={})

    @asynccontextmanager
    async def use(self, role, on_load=None):
        assert role == "embedding"
        if self.down:
            raise ConflictError("no active model profile for role 'embedding'")
        yield SimpleNamespace(adapter=self.adapter, profile=self.profile)


class FakeKnowledgeRepo:
    def __init__(self, mgr: FakeEmbedManager):
        self.mgr = mgr
        self.docs: dict[UUID, dict] = {}
        self.chunks: dict[UUID, dict] = {}
        self.emb: dict[tuple[UUID, UUID], list[float]] = {}
        self.jobs: dict[UUID, ReindexJob] = {}

    async def active_embedding_profile_id(self):
        return self.mgr.profile.id

    async def find_by_checksum(self, checksum):
        return next((i for i, d in self.docs.items() if d["checksum"] == checksum), None)

    async def add_document(self, meta, chunks):
        if await self.find_by_checksum(meta["checksum"]):
            raise ConflictError("document already ingested (same checksum)")
        did = uuid4()
        self.docs[did] = {**meta, "id": did, "ingested_at": _now()}
        for c in chunks:
            self.chunks[uuid4()] = {
                "document_id": did,
                "chunk_index": c.chunk_index,
                "content": c.content,
                "metadata": c.metadata,
            }
        return did

    def _doc(self, did, pid) -> Document:
        d = self.docs[did]
        ids = [i for i, c in self.chunks.items() if c["document_id"] == did]
        return Document(
            **{**d, "document_type": d["category"]},
            chunk_count=len(ids),
            embedded_count=sum((i, pid) in self.emb for i in ids),
        )

    async def list_documents(self, profile_id):
        return [self._doc(i, profile_id) for i in self.docs]

    async def get_document(self, id, profile_id):
        if id not in self.docs:
            return None
        cs = sorted((i, c) for i, c in self.chunks.items() if c["document_id"] == id)
        prev = [
            ChunkOut(id=i, chunk_index=c["chunk_index"], content=c["content"], metadata=c["metadata"])
            for i, c in cs[:5]
        ]
        return DocumentDetail(**self._doc(id, profile_id).model_dump(), metadata={}, preview=prev)

    async def document_text(self, id):
        d = self.docs.get(id)
        if d is None:
            return None
        cs = sorted((c for c in self.chunks.values() if c["document_id"] == id), key=lambda c: c["chunk_index"])
        chunks = [
            {"chunk_index": c["chunk_index"], "section": c["metadata"].get("section"), "content": c["content"]}
            for c in cs
        ]
        return {**d, "document_type": d["category"], "full_text": d.get("full_text"), "chunks": chunks}

    async def set_full_text(self, id, text):
        self.docs[id]["full_text"] = text

    async def delete_document(self, id):
        if id not in self.docs:
            return False
        del self.docs[id]
        self.chunks = {i: c for i, c in self.chunks.items() if c["document_id"] != id}
        return True

    async def count_chunks(self):
        return len(self.chunks)

    async def count_embedded(self, profile_id):
        return sum(p == profile_id for _, p in self.emb)

    def _pending(self, pid, did):
        return [
            (i, c)
            for i, c in self.chunks.items()
            if (i, pid) not in self.emb and (did is None or c["document_id"] == did)
        ]

    async def count_pending(self, profile_id, document_id=None):
        return len(self._pending(profile_id, document_id))

    async def chunks_to_embed(self, profile_id, limit, document_id=None):
        return [
            PendingChunk(i, self.docs[c["document_id"]]["title"], c["content"])
            for i, c in self._pending(profile_id, document_id)[:limit]
        ]

    async def save_embeddings(self, profile_id, items):
        for cid, v in items:
            self.emb[(cid, profile_id)] = v

    async def ensure_index(self, profile_id, dim):
        pass

    async def rank_settings(self):
        return {}

    async def search(self, profile_id, dim, vector, top_k, filters, rank=None):
        hits = []
        for (cid, pid), v in self.emb.items():
            if pid != profile_id:
                continue
            c = self.chunks[cid]
            d = self.docs[c["document_id"]]
            if any({"document_type": d["category"]}.get(k, d.get(k)) != val for k, val in filters.items()):
                continue
            score = sum(a * b for a, b in zip(vector, v, strict=False))
            hits.append(
                KnowledgeHit(
                    chunk_id=str(cid),
                    document_id=str(d["id"]),
                    title=d["title"],
                    content=c["content"],
                    source_uri=d["source_uri"],
                    section=c["metadata"].get("section"),
                    score=score,
                    is_synthetic=d["is_synthetic"],
                )
            )
        return sorted(hits, key=lambda h: -h.score)[:top_k]

    async def create_job(self, to_profile_id):
        j = ReindexJob(
            id=uuid4(),
            from_profile_id=None,
            to_profile_id=to_profile_id,
            status="pending",
            progress=0,
            error=None,
            started_at=None,
            finished_at=None,
            created_at=_now(),
        )
        self.jobs[j.id] = j
        return j

    async def update_job(self, id, **fields):
        self.jobs[id] = self.jobs[id].model_copy(update=fields)
        return self.jobs[id]

    async def get_job(self, id):
        return self.jobs.get(id)

    async def latest_job(self):
        return max(self.jobs.values(), key=lambda j: j.created_at, default=None)

    async def active_job(self):
        return next((j for j in self.jobs.values() if j.status in ("pending", "running")), None)


# ---------------------------------------------------------------- tickets
def unit(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))
