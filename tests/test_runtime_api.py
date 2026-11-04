import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.config.deps import get_config_service
from app.config.service import ConfigService
from app.main import create_app
from app.models_runtime.deps import get_model_manager
from app.models_runtime.manager import ModelManager
from app.shared.crypto import SecretCipher
from tests.fakes import FakeStore, FakeUnitOfWork
from tests.fakes_runtime import FakeAdapter, FakeRuntimeStore
from tests.test_api import mk_profile, mk_provider

SIZES = {"a": 400, "b": 400, "c": 400, "big": 5000, "emb": 300}


@pytest.fixture
async def rt():
    store = FakeStore()
    store.settings.update(ram_budget_mb=1000, strict_single_model=False)
    adapters: dict = {}
    mgr = ModelManager(
        FakeRuntimeStore(store),
        lambda p, token: adapters.setdefault(p.id, FakeAdapter(p.kind, SIZES)),
        SecretCipher(Fernet.generate_key()).decrypt,
    )
    app = create_app()
    svc = ConfigService(lambda: FakeUnitOfWork(store), SecretCipher(Fernet.generate_key()))
    app.dependency_overrides[get_config_service] = lambda: svc
    app.dependency_overrides[get_model_manager] = lambda: mgr
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.store, c.adapters, c.mgr = store, adapters, mgr  # type: ignore[attr-defined]
        yield c


async def prof(c, pid, ref, role="llm", mb=None):
    p = await mk_profile(c, pid, name=f"p-{ref}", role=role, model_ref=ref, est_memory_mb=mb or SIZES[ref])
    return p["id"]


def names(r):
    return sorted(m["name"] for m in r.json()["loaded"])


async def test_status_shape(rt):
    prov = await mk_provider(rt)
    r = await rt.get("/api/runtime/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ram_budget_mb"] == 1000 and body["strict_single_model"] is False
    assert body["used_mb"] == 0 and body["loaded"] == []
    assert body["providers"][0]["provider_id"] == prov["id"]
    assert body["providers"][0]["reachable"] is True and isinstance(body["providers"][0]["latency_ms"], int)


async def test_load_evicts_lru_when_over_budget(rt):
    pid = (await mk_provider(rt))["id"]
    a, b, c = [await prof(rt, pid, r) for r in "abc"]
    for x in (a, b):
        assert (await rt.post(f"/api/runtime/profiles/{x}/load")).json()["evicted"] == []
    await rt.post(f"/api/runtime/profiles/{a}/load")  # touch a -> b is now least recently used
    r = await rt.post(f"/api/runtime/profiles/{c}/load")
    assert r.status_code == 200 and r.json()["evicted"] == ["p-b"]
    assert names(r) == ["p-a", "p-c"] and r.json()["used_mb"] == 800
    assert [e[1] for e in rt.mgr._store.events] == ["load", "load", "evict", "load"]


async def test_strict_single_model_unloads_others(rt):
    rt.store.settings["strict_single_model"] = True
    pid = (await mk_provider(rt))["id"]
    a, b = await prof(rt, pid, "a"), await prof(rt, pid, "b")
    await rt.post(f"/api/runtime/profiles/{a}/load")
    r = await rt.post(f"/api/runtime/profiles/{b}/load")
    assert r.json()["evicted"] == ["p-a"] and names(r) == ["p-b"]
    assert list(rt.adapters.values())[0].loaded == {"b": 400}


async def test_model_bigger_than_budget_conflicts_unless_alone(rt):
    pid = (await mk_provider(rt))["id"]
    a, big = await prof(rt, pid, "a"), await prof(rt, pid, "big")
    await rt.post(f"/api/runtime/profiles/{a}/load")
    r = await rt.post(f"/api/runtime/profiles/{big}/load")
    assert r.status_code == 409 and "budget" in r.json()["detail"]
    await rt.post(f"/api/runtime/profiles/{a}/unload")
    r = await rt.post(f"/api/runtime/profiles/{big}/load")  # nothing else loaded -> allowed with warning
    assert r.status_code == 200 and r.json()["warning"] == "exceeds_ram_budget"


async def test_embedding_probe_reports_dimension_and_memory(rt):
    pid = (await mk_provider(rt))["id"]
    e = await prof(rt, pid, "emb", role="embedding")
    r = await rt.post(f"/api/runtime/profiles/{e}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["dimension"] == 768 and body["measured_memory_mb"] == 300
    assert (await rt.get(f"/api/model-profiles/{e}")).status_code == 200 and next(
        iter(rt.store.profiles.values())
    ).measured_memory_mb == 300


async def test_pull_rejects_non_ollama(rt):
    pid = (await mk_provider(rt, name="llama", kind="llamacpp"))["id"]
    r = await rt.post(f"/api/runtime/providers/{pid}/pull", json={"model_ref": "x"})
    assert r.status_code == 422 and "ollama" in r.json()["detail"]


async def test_pull_streams_sse_for_ollama(rt):
    pid = (await mk_provider(rt))["id"]
    r = await rt.post(f"/api/runtime/providers/{pid}/pull", json={"model_ref": "gemma"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert '"status": "pulling"' in r.text and "event: done" in r.text


async def test_unreachable_provider_does_not_break_status(rt):
    await mk_provider(rt)
    await rt.get("/api/runtime/status")
    list(rt.adapters.values())[0].up = False
    r = await rt.get("/api/runtime/status")
    assert r.status_code == 200 and r.json()["providers"][0]["reachable"] is False
