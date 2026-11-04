from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.router import get_agent_service
from app.agent.service import AgentService
from app.main import create_app

SID = uuid4()
YAML = (Path(__file__).parent.parent / "rules" / "diagnostico_red.v1.yaml").read_text()


class FakeAgentRepo:
    def __init__(self):
        self.facts: list[dict] = []
        self.hyps: list[dict] = []
        self.trace: list[dict] = []
        self.case: dict | None = None
        self.rule_sets = [{"version": 1, "name": "diagnostico_red", "content_yaml": YAML, "is_active": True,
                           "created_at": datetime.now(UTC)}]  # fmt: skip

    async def session_exists(self, sid):
        return sid == SID

    async def get_case(self, sid):
        return self.case

    async def ensure_case(self, sid):
        self.case = self.case or {"state": "nuevo", "goal": "restablecer_conectividad", "subgoals": [],
                                  "verified": False, "ticket_id": None}  # fmt: skip
        return self.case

    async def active_facts(self, sid):
        return [f for f in self.facts if f["superseded"] is False]

    async def upsert_fact(self, sid, key, value, source, certainty, evidence_ref=None, turn_id=None):
        for f in self.facts:
            if f["key"] == key:
                f["superseded"] = True
        f = {"key": key, "value": value, "source": source, "certainty": certainty, "evidence_ref": None,
             "turn_id": None, "created_at": datetime.now(UTC), "superseded": False}  # fmt: skip
        self.facts.append(f)
        return f

    async def get_hypotheses(self, sid):
        return self.hyps

    async def replace_hypotheses(self, sid, items):
        self.hyps = list(items)

    async def append_trace(self, sid, phase, *, facts_used, rules_fired, hypotheses, action=None, autonomy=None,
                           reason=None, turn_id=None):  # fmt: skip
        self.trace.append({"id": uuid4(), "phase": phase, "facts_used": facts_used, "rules_fired": rules_fired,
                           "hypotheses": hypotheses, "action": action, "autonomy": autonomy, "reason": reason,
                           "turn_id": turn_id, "created_at": datetime.now(UTC)})  # fmt: skip

    async def actions_taken(self, sid):
        return [t["action"] for t in self.trace if t["phase"] == "actuar" and t["action"]]

    async def update_case(self, sid, *, state=None, verified=None, ticket_number=None):
        c = await self.ensure_case(sid)
        c["state"] = state or c["state"]
        c["verified"] = c["verified"] if verified is None else verified
        if ticket_number:
            c["ticket_id"], c["ticket_number"] = uuid4(), ticket_number

    async def list_trace(self, sid, limit=50):
        return self.trace[-limit:]

    async def latest_trace(self, sid, phase):
        return next((t for t in reversed(self.trace) if t["phase"] == phase), None)

    async def get_setting(self, key):
        return None

    async def active_rule_set(self):
        return next((r for r in self.rule_sets if r["is_active"]), None)

    async def list_rule_sets(self):
        return [{k: v for k, v in r.items() if k != "content_yaml"} for r in reversed(self.rule_sets)]

    async def get_rule_set(self, version):
        return next((r for r in self.rule_sets if r["version"] == version), None)

    async def create_rule_set(self, content_yaml):
        r = {"version": len(self.rule_sets) + 1, "name": "diagnostico_red", "content_yaml": content_yaml,
             "is_active": False, "created_at": datetime.now(UTC)}  # fmt: skip
        self.rule_sets.append(r)
        return r

    async def activate_rule_set(self, version):
        for r in self.rule_sets:
            r["is_active"] = r["version"] == version


@pytest.fixture
async def client():
    app = create_app()
    repo = FakeAgentRepo()
    app.dependency_overrides[get_agent_service] = lambda: AgentService(repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _fact(c, key, value, sid: UUID = SID):
    return await c.post(f"/api/cases/{sid}/facts", json={"key": key, "value": value})


async def test_validate_ok_and_invalid(client):
    ok = (await client.post("/api/rules/validate", json={"content_yaml": YAML})).json()
    assert ok == {"ok": True, "errors": []}
    bad = YAML.replace("{otros_equipos_navegan: false},", "{fantasma: false},", 1).replace(
        "H1_caida_proveedor: +0.5", "H1_caida_proveedor: +1.5", 1
    )
    res = (await client.post("/api/rules/validate", json={"content_yaml": bad})).json()
    assert res["ok"] is False
    paths = {e["path"] for e in res["errors"]}
    assert "rules.R1.when.fantasma" in paths and "rules.R1.then.H1_caida_proveedor" in paths


async def test_facts_r1_r3_accept_h1(client):
    assert (await _fact(client, "otros_equipos_navegan", False)).status_code == 200
    r = await _fact(client, "luz_modem", "roja")
    assert r.status_code == 200
    case = (await client.get(f"/api/cases/{SID}")).json()
    h = {x["key"]: x for x in case["hypotheses"]}
    assert h["H1_caida_proveedor"]["cf"] >= 0.8 and h["H1_caida_proveedor"]["status"] == "aceptada"
    assert h["H2_solo_dispositivo"]["status"] == "descartada"
    assert case["trace"][-1]["rules_fired"] == ["R1", "R3"] and case["trace"][-1]["phase"] == "creer"
    assert (await client.get(f"/api/cases/{uuid4()}")).status_code == 404
    assert (await _fact(client, "luz_modem", "morada")).status_code == 422


async def test_explain_uses_rule_text(client):
    await _fact(client, "otros_equipos_navegan", False)
    await _fact(client, "luz_modem", "roja")
    ex = (await client.get(f"/api/cases/{SID}/explain", params={"target": "H1_caida_proveedor"})).json()
    assert ex["confidence"] == "alta" and [r["id"] for r in ex["rules"]] == ["R1", "R3"]
    assert "ningún otro equipo navega" in ex["text"] and "la luz del módem está roja o naranja" in ex["text"]
    assert (await client.get(f"/api/cases/{SID}/explain", params={"target": "nope"})).status_code == 404


async def test_activate_invalid_version_422(client):
    bad = YAML.replace("H1_caida_proveedor: +0.5", "H9: +0.5", 1)
    created = (await client.post("/api/rules", json={"content_yaml": bad})).json()
    assert created["version"] == 2 and created["ok"] is False
    assert (await client.post("/api/rules/2/activate")).status_code == 422
    assert (await client.post("/api/rules/3/activate")).status_code == 404
    assert (await client.post("/api/rules", json={"content_yaml": YAML})).status_code == 201
    assert (await client.post("/api/rules/3/activate")).json() == {"version": 3, "is_active": True}
    assert [r["is_active"] for r in (await client.get("/api/rules")).json()] == [True, False, False]
