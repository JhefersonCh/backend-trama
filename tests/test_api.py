import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.config.deps import get_config_service
from app.config.service import ConfigService
from app.main import create_app, get_db_check
from app.shared.crypto import SecretCipher
from tests.fakes import FakeStore, FakeUnitOfWork


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
async def client(store):
    app = create_app()
    svc = ConfigService(lambda: FakeUnitOfWork(store), SecretCipher(Fernet.generate_key()))
    app.dependency_overrides[get_config_service] = lambda: svc
    app.state.db_ok = True

    async def ok():
        return app.state.db_ok

    app.dependency_overrides[get_db_check] = lambda: ok
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.app = app
        yield c


async def mk_provider(c, name="ollama-local", **kw):
    r = await c.post("/api/providers", json={"name": name, "kind": "ollama", "base_url": "http://ollama:11434", **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def mk_profile(c, pid, name="p1", role="llm", **kw):
    r = await c.post(
        "/api/model-profiles", json={"name": name, "role": role, "provider_id": pid, "model_ref": "gemma", **kw}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_health_ok_and_503(client):
    assert (await client.get("/health")).json() == {"status": "ok", "db": "ok"}
    client.app.state.db_ok = False
    r = await client.get("/health")
    assert r.status_code == 503 and r.json()["db"] == "error"


async def test_provider_crud_and_duplicate(client):
    p = await mk_provider(client)
    assert p["has_secret"] is False and p["enabled"] is True
    assert (await client.get("/api/providers")).json()[0]["id"] == p["id"]
    r = await client.patch(f"/api/providers/{p['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert (
        await client.post("/api/providers", json={"name": "ollama-local", "kind": "ollama", "base_url": "x"})
    ).status_code == 409
    assert (await client.post("/api/providers", json={"name": "z", "kind": "bad", "base_url": "x"})).status_code == 422
    assert (await client.get("/api/providers/00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await client.delete(f"/api/providers/{p['id']}")).status_code == 204


async def test_secret_never_returned_and_audited_without_value(client):
    p = await mk_provider(client)
    r = await client.put(f"/api/providers/{p['id']}/secret", json={"value": "sk-TOPSECRET"})
    assert r.status_code == 204
    got = await client.get(f"/api/providers/{p['id']}")
    assert got.json()["has_secret"] is True
    assert "sk-TOPSECRET" not in got.text and "secret" not in got.json()
    assert (await client.delete(f"/api/providers/{p['id']}/secret")).status_code == 204
    assert (await client.get(f"/api/providers/{p['id']}")).json()["has_secret"] is False
    audit = (await client.get("/api/config-audit")).json()
    assert {"secret_set", "secret_deleted"} <= {a["action"] for a in audit}
    assert "sk-TOPSECRET" not in str(audit)
    assert (await client.put(f"/api/providers/{p['id']}/secret", json={"value": ""})).status_code == 422


async def test_delete_provider_with_profiles_is_409(client):
    p = await mk_provider(client)
    await mk_profile(client, p["id"])
    assert (await client.delete(f"/api/providers/{p['id']}")).status_code == 409


async def test_profile_create_inactive_patch_filter_delete(client):
    p = await mk_provider(client)
    pr = await mk_profile(client, p["id"], est_memory_mb=3000)
    assert pr["is_active"] is False and pr["params"] == {}
    assert (await client.patch(f"/api/model-profiles/{pr['id']}", json={"is_active": True})).status_code == 422
    r = await client.patch(f"/api/model-profiles/{pr['id']}", json={"model_ref": "gemma2"})
    assert r.json()["model_ref"] == "gemma2"
    assert len((await client.get("/api/model-profiles?role=llm")).json()) == 1
    assert (await client.get("/api/model-profiles?role=tts")).json() == []
    assert (await client.delete(f"/api/model-profiles/{pr['id']}")).status_code == 204


async def test_activate_switches_active_and_blocks_delete(client):
    p = await mk_provider(client)
    a = await mk_profile(client, p["id"], "a")
    b = await mk_profile(client, p["id"], "b")
    r = await client.post(f"/api/model-profiles/{a['id']}/activate")
    assert r.status_code == 200 and r.json()["profile"]["is_active"] is True
    assert r.json()["reindex_required"] is False
    await client.post(f"/api/model-profiles/{b['id']}/activate")
    active = {x["name"]: x["is_active"] for x in (await client.get("/api/model-profiles")).json()}
    assert active == {"a": False, "b": True}
    assert (await client.delete(f"/api/model-profiles/{b['id']}")).status_code == 409


async def test_activate_embedding_reindex_and_disabled_provider(client):
    p = await mk_provider(client)
    a = await mk_profile(client, p["id"], "e1", "embedding", params={"dimension": 768})
    b = await mk_profile(client, p["id"], "e2", "embedding", params={"dimension": 1024})
    await client.post(f"/api/model-profiles/{a['id']}/activate")
    r = await client.post(f"/api/model-profiles/{b['id']}/activate")
    assert r.json()["reindex_required"] is True
    # same dimension, different model -> incompatible vector spaces -> reindex
    d = await mk_profile(client, p["id"], "e3", "embedding", model_ref="other-embedder", params={"dimension": 1024})
    r = await client.post(f"/api/model-profiles/{d['id']}/activate")
    assert r.json()["reindex_required"] is True
    off = await mk_provider(client, "off", enabled=False)
    c = await mk_profile(client, off["id"], "c")
    assert (await client.post(f"/api/model-profiles/{c['id']}/activate")).status_code == 409


async def test_settings_valid_and_invalid(client):
    r = await client.put("/api/settings/ram_budget_mb", json={"value": 8192})
    assert r.status_code == 200 and r.json() == {"key": "ram_budget_mb", "value": 8192}
    ok = await client.put("/api/settings/wake_word", json={"value": {"enabled": True, "phrase": "hey trama"}})
    assert ok.status_code == 200
    assert (await client.get("/api/settings")).json()["ram_budget_mb"] == 8192
    for key, val in [
        ("ram_budget_mb", 100),
        ("ram_budget_mb", True),
        ("nope", 1),
        ("wake_word", {"enabled": True, "phrase": ""}),
        ("vad", {"positive_threshold": 2, "min_silence_ms": 500}),
        ("system_prompt", "  "),
    ]:
        r = await client.put(f"/api/settings/{key}", json={"value": val})
        assert r.status_code == 422, (key, val)
        assert "detail" in r.json()


async def test_audit_records_mutations_and_limit(client):
    p = await mk_provider(client)
    await client.patch(f"/api/providers/{p['id']}", json={"enabled": False})
    await client.put("/api/settings/rag_top_k", json={"value": 5})
    log = (await client.get("/api/config-audit")).json()
    assert [a["action"] for a in log] == ["update", "update", "create"]
    assert len((await client.get("/api/config-audit?limit=1")).json()) == 1


async def test_rag_settings_validation(client):
    for key, val in [("rag_mode", "both"), ("rag_min_score", 0.6)]:
        r = await client.put(f"/api/settings/{key}", json={"value": val})
        assert r.status_code == 200 and r.json() == {"key": key, "value": val}
    for key, val in [("rag_mode", "always"), ("rag_mode", 1), ("rag_min_score", 1.5), ("rag_min_score", "0.5")]:
        assert (await client.put(f"/api/settings/{key}", json={"value": val})).status_code == 422, (key, val)


async def test_warmup_on_start_setting_validation(client):
    assert (await client.put("/api/settings/warmup_on_start", json={"value": False})).status_code == 200
    assert (await client.put("/api/settings/warmup_on_start", json={"value": "yes"})).status_code == 422
