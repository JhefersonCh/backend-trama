# ruff: noqa: E501
"""Configurable tools: CRUD/toggle/intents, SQL guard, anti-SSRF HTTP, static templates, test endpoint."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config.errors import ConflictError, NotFoundError
from app.main import create_app
from app.tools.builtin import build_registry
from app.tools.declarative import DeclarativeRunner
from app.tools.netguard import BlockedRequestError, safe_fetch
from app.tools.registry import ToolContext
from app.tools.router import get_tools_service
from app.tools.service import RateLimiter, ToolsService
from app.tools.sqlguard import SqlRejectedError, validate_select
from tests.fakes_conversation import FakeAudit, FakeDiagnostics, FakeInventory, FakeKnowledge, FakeTickets

SCHEMA = {"type": "object", "properties": {"dia": {"type": "string"}}, "required": ["dia"]}
TPL = {"name": "horario", "kind": "static_template", "description": "Horario de soporte", "params_schema": SCHEMA,
       "config": {"template": "Atendemos el {{ dia }} de 8 a 17"}, "intents": ["smalltalk"]}  # fmt: skip


class FakeRepo:
    def __init__(self):
        self.rows: dict[UUID, dict] = {}
        self.versions_: dict[UUID, list[dict]] = {}
        self.sql: list[tuple] = []

    def _snap(self, r):
        self.versions_.setdefault(r["id"], []).append(
            {"version": r["version"], "snapshot": dict(r), "created_at": datetime.now(UTC)}
        )

    async def list_tools(self):
        return sorted((dict(r) for r in self.rows.values()), key=lambda r: r["name"])

    async def get(self, id):
        if id not in self.rows:
            raise NotFoundError("herramienta no encontrada")
        return dict(self.rows[id])

    async def insert(self, d):
        if any(r["name"] == d["name"] for r in self.rows.values()):
            raise ConflictError("duplicada")
        r = {
            **d,
            "id": uuid4(),
            "version": 1,
            "updated_at": datetime.now(UTC),
            "usage": {"calls": 0, "errors": 0, "last_used_at": None},
        }
        self.rows[r["id"]] = r
        self._snap(r)
        return dict(r)

    async def update(self, id, d):
        r = await self.get(id)
        r.update(d, version=r["version"] + 1)
        self.rows[id] = r
        self._snap(r)
        return dict(r)

    async def set_enabled(self, id, enabled):
        self.rows[id]["enabled"] = enabled
        return await self.get(id)

    async def versions(self, id):
        await self.get(id)
        return list(reversed(self.versions_[id]))

    async def run_select(self, sql, params):
        self.sql.append((sql, params))
        return [{"name": "Laura"}]

    async def secret_ciphertext(self, ref):
        return b"cipher" if ref == "provider:api" else None


@pytest.fixture
async def env():
    repo, audit = FakeRepo(), FakeAudit()
    registry = build_registry(knowledge=FakeKnowledge(), tickets=FakeTickets(), inventory=FakeInventory(),
                              diagnostics=FakeDiagnostics(), audit=audit)  # fmt: skip
    registry.attach(repo, lambda b: "s3cr3t")
    svc = ToolsService(repo, registry, RateLimiter())
    app: FastAPI = create_app()
    app.dependency_overrides[get_tools_service] = lambda: svc
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, registry, repo, audit


async def test_crud_toggle_versions_and_definitions_for(env):
    c, reg, repo, _ = env
    r = await c.post("/api/tools", json=TPL)
    assert r.status_code == 201 and r.json()["version"] == 1
    tid = r.json()["id"]
    assert "horario" in reg.names()
    assert [d["function"]["name"] for d in reg.definitions_for("smalltalk")] == ["horario"]
    assert "horario" not in [d["function"]["name"] for d in reg.definitions_for("ticket")]  # intent mismatch
    assert (await c.post("/api/tools", json=TPL)).status_code == 409
    assert (await c.post("/api/tools", json={**TPL, "name": "x", "kind": "builtin"})).status_code == 422
    p = await c.patch(f"/api/tools/{tid}", json={"description": "Horario nuevo", "intents": ["closing"]})
    assert p.json()["version"] == 2 and len((await c.get(f"/api/tools/{tid}/versions")).json()) == 2
    assert reg.definitions_for("smalltalk") == [] and len(reg.definitions_for("closing")) == 1
    assert (await c.post(f"/api/tools/{tid}/toggle")).json()["enabled"] is False
    assert "horario" not in reg.names() and reg.definitions_for("closing") == []
    res = await reg.execute("horario", {"dia": "lunes"}, ToolContext(None))
    assert not res.ok and res.status == "denied"


async def test_sql_readonly_guard():
    ok = "select name, level from v_tools_technicians_available where %(zona)s = any(zones) order by name limit 20"
    assert validate_select(ok) == {"zona"}
    bad = [
        "select 1; drop table tickets",
        "select * from tickets limit 5",
        "select * from v_tools_technicians_available limit 500",
        "select * from v_tools_technicians_available",
        "DROP TABLE technicians",
        "select pg_sleep(5) from v_tools_technicians_available limit 1",
        "select * from v_tools_technicians_available, tickets limit 1",
        "select * from v_tools_technicians_available where name = 'x' -- c\nlimit 1",
        "select * from v_tools_technicians_available where name = %s limit 1",
        "select * from pg_catalog.pg_user limit 1",
        "select * from v_tools_technicians_available where zones && (select zones from tickets) limit 1",
    ]
    for q in bad:
        with pytest.raises(SqlRejectedError):
            validate_select(q)


async def test_http_request_blocks_ssrf_and_redirects():
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        if req.url.path == "/redir":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        return httpx.Response(200, json={"status": "ok"})

    async def public(host, port):
        return ["93.184.216.34"]

    async def to_loopback(host, port):
        return ["127.0.0.1"]

    kw = {
        "allowed_hosts": ["api.example.com", "127.0.0.1", "10.0.0.1", "169.254.169.254"],
        "transport": httpx.MockTransport(handler),
    }
    for url in (
        "https://127.0.0.1/x",
        "https://10.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/",
    ):
        with pytest.raises(BlockedRequestError):
            await safe_fetch("GET", url, resolver=public, **kw)
    with pytest.raises(BlockedRequestError):  # DNS pointing to loopback
        await safe_fetch("GET", "https://api.example.com/", resolver=to_loopback, **kw)
    with pytest.raises(BlockedRequestError):  # host not in allowlist / plain http
        await safe_fetch("GET", "https://evil.com/", resolver=public, **kw)
    with pytest.raises(BlockedRequestError):
        await safe_fetch("GET", "http://api.example.com/", resolver=public, **kw)
    with pytest.raises(BlockedRequestError, match="redirecci"):
        await safe_fetch("GET", "https://api.example.com/redir", resolver=public, **kw)
    status, _, body = await safe_fetch("GET", "https://api.example.com/ok", resolver=public, **kw)
    assert status == 200 and hits == ["https://93.184.216.34/redir", "https://93.184.216.34/ok"]  # pinned IP

    runner = DeclarativeRunner(
        repo=FakeRepo(), decrypt=lambda b: "s3cr3t", transport=httpx.MockTransport(handler), resolver=public
    )
    out = await runner.run(
        {
            "kind": "http_request",
            "config": {
                "url": "https://api.example.com/ok?z={{ zona }}",
                "allowed_hosts": ["api.example.com"],
                "response_path": "$.status",
                "secret_ref": "provider:api",
            },
        },
        {"zona": "a b"},
        ToolContext(None),
    )
    assert out.data["result"] == "ok"


async def test_static_template_renders_sandboxed(env):
    c, reg, _, audit = env
    await c.post("/api/tools", json=TPL)
    r = await reg.execute("horario", {"dia": "lunes"}, ToolContext(None))
    assert r.ok and "lunes" in r.data["text"] and audit.rows[-1][0] == "horario"
    assert not (await reg.execute("horario", {}, ToolContext(None))).ok  # jsonschema: dia required
    evil = {**TPL, "name": "evil", "config": {"template": "{{ ''.__class__.__mro__ }}"}}
    created = await c.post("/api/tools", json=evil)
    out = await c.post(f"/api/tools/{created.json()['id']}/test", json={"args": {"dia": "x"}})
    assert out.json()["ok"] is False


async def test_test_endpoint_and_rate_limit(env):
    c, _, repo, _ = env
    tid = (await c.post("/api/tools", json={**TPL, "enabled": False})).json()["id"]
    r = await c.post(f"/api/tools/{tid}/test", json={"args": {"dia": "martes"}})
    body = r.json()
    assert r.status_code == 200 and body["ok"] and "martes" in body["output"]["text"] and body["duration_ms"] >= 0
    codes = [(await c.post(f"/api/tools/{tid}/test", json={"args": {"dia": "x"}})).status_code for _ in range(10)]
    assert codes[-1] == 429
    sq = {"name": "tecnicos", "kind": "sql_readonly", "description": "Técnicos por zona", "intents": ["ticket"],
          "params_schema": {"type": "object", "properties": {"zona": {"type": "string"}}, "required": ["zona"]},
          "config": {"query": "select name from v_tools_technicians_available where %(zona)s = any(zones) limit 20"}}  # fmt: skip
    assert (await c.post("/api/tools", json=sq)).status_code == 201
    assert (
        await c.post("/api/tools", json={**sq, "name": "mala", "config": {"query": "delete from tickets"}})
    ).status_code == 422


async def test_builtin_rows_only_edit_description_intents_enabled(env):
    c, reg, repo, _ = env
    row = await repo.insert({"name": "get_ticket_status", "kind": "builtin", "description": "Estado", "params_schema": {"type": "object", "properties": {}}, "config": {}, "intents": ["ticket"], "enabled": True})  # fmt: skip
    await reg.refresh()
    assert (await c.patch(f"/api/tools/{row['id']}", json={"config": {"x": 1}})).status_code == 422
    r = await c.patch(f"/api/tools/{row['id']}", json={"description": "Estado del ticket TRM"})
    assert r.status_code == 200
    assert reg.definitions_for("ticket")[0]["function"]["description"] == "Estado del ticket TRM"
