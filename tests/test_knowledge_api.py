import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.knowledge.deps import get_knowledge_service, get_search_service
from app.knowledge.service import KnowledgeSearchService, KnowledgeService
from app.knowledge.wiring import router
from app.main import create_app
from tests.fakes_knowledge import FakeEmbedManager, FakeKnowledgeRepo

MD = (
    "# Guía DHCP\n\n## APIPA\n\nSi un equipo muestra la dirección 169.254.10.5 no obtuvo concesión DHCP. "
    "Revisar el servidor DHCP.\n\n## DNS\n\nSi no resuelven nombres, probar con nslookup y revisar los DNS.\n"
).encode()


@pytest.fixture
async def kc():
    mgr = FakeEmbedManager()
    repo = FakeKnowledgeRepo(mgr)
    app = create_app()
    app.include_router(router)  # idempotent if main.py already wires it
    app.dependency_overrides[get_knowledge_service] = lambda: KnowledgeService(repo, mgr)
    app.dependency_overrides[get_search_service] = lambda: KnowledgeSearchService(repo, mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.mgr, c.repo = mgr, repo  # type: ignore[attr-defined]
        yield c


async def upload(c, content=MD, name="guia.md", **form):
    return await c.post("/api/knowledge/documents", files={"file": (name, content, "text/markdown")}, data=form)


async def test_upload_markdown_chunks_and_embeds(kc):
    r = await upload(kc, publisher="TRAMA", document_type="guide")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["chunks"] >= 2 and body["embedded"] == body["chunks"] and body["warning"] is None
    assert body["document"]["title"] == "Guía DHCP" and len(body["document"]["checksum"]) == 64
    assert (await kc.get("/api/knowledge/documents")).json()[0]["embedded_count"] == body["chunks"]


async def test_duplicate_upload_conflict(kc):
    assert (await upload(kc)).status_code == 201
    r = await upload(kc)
    assert r.status_code == 409 and "checksum" in r.json()["detail"]


async def test_search_orders_by_score(kc):
    await upload(kc)
    r = await kc.post("/api/knowledge/search", json={"query": "dirección 169.254 sin DHCP", "top_k": 5})
    assert r.status_code == 200
    hits = r.json()
    assert len(hits) >= 2
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert "169.254" in hits[0]["content"] and hits[0]["section"] == "Guía DHCP > APIPA"


async def test_search_without_embeddings_for_active_profile_409(kc):
    await upload(kc)
    kc.mgr.switch_profile()
    r = await kc.post("/api/knowledge/search", json={"query": "dhcp"})
    assert r.status_code == 409 and "reindex" in r.json()["detail"]


async def test_reindex_job_completes_and_enables_search(kc):
    await upload(kc)
    kc.mgr.switch_profile()
    r = await kc.post("/api/knowledge/reindex")
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(50):
        job = (await kc.get(f"/api/knowledge/reindex/{job_id}")).json()
        if job["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    assert job["status"] == "completed" and job["progress"] == 100
    assert (await kc.get("/api/knowledge/reindex/latest")).json()["id"] == job_id
    assert (await kc.post("/api/knowledge/search", json={"query": "dns"})).status_code == 200


async def test_content_rebuilds_from_chunks_when_full_text_is_null(kc):
    doc_id = (await upload(kc)).json()["document"]["id"]
    for d in kc.repo.docs.values():  # simulate a document ingested before migration 011
        d["full_text"] = None
    r = await kc.get(f"/api/knowledge/documents/{doc_id}/content")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "APIPA" in body["full_text"] and "169.254.10.5" in body["full_text"] and body["language"] == "es"
    first = body["sections"][0]
    assert first["start"] is not None and body["full_text"][first["start"] : first["end"]]
    assert next(iter(kc.repo.docs.values()))["full_text"] == body["full_text"]  # backfilled
