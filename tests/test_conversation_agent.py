# ruff: noqa: E501
"""Agent core inside the conversation turn (PART 2). Fakes only: no DB, no model containers, no live LLM."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.service import AgentService
from app.config.errors import NotFoundError
from app.conversation.deps import get_conversation_service
from app.conversation.router import router
from app.conversation.service import ConversationService
from app.runtimes.base import ChatDelta
from app.tickets.models import TicketSummary
from app.tools.builtin import build_registry
from tests.fakes_conversation import (
    FakeAudit,
    FakeDiagnostics,
    FakeInventory,
    FakeKnowledge,
    FakeManager,
    FakeRepo,
    FakeSettings,
    FakeTickets,
    say,
)
from tests.test_agent_api import FakeAgentRepo

PHONE = "310 555 1234"


class AgentRepo(FakeAgentRepo):
    async def session_exists(self, sid):
        return True


class CaseTickets(FakeTickets):
    def __init__(self, with_ticket: bool = True):
        self.created: list[dict] = []
        self.contacts: list[tuple[str, str]] = []
        self.with_ticket = with_ticket

    def _timeline(self, number):
        now = datetime.now(UTC)
        return SimpleNamespace(
            ticket_number=number, status="assigned", age_seconds=7200,
            technician=SimpleNamespace(name="Laura Gómez", specialties=["fibra"]),
            last_note=SimpleNamespace(text="Voy en camino", technician_name="Laura Gómez", at=now),
            sla_remaining_seconds=5 * 3600, queued=False,
        )  # fmt: skip

    async def create_incident_with_ticket(self, **kw):
        self.created.append(kw)
        return TicketSummary(ticket_number="TRM-000007", status="assigned", priority=kw["priority"], title=kw["title"],
                             created_at=datetime.now(UTC), technician_name="Laura Gómez",
                             sla_due_at=datetime.now(UTC) + timedelta(hours=8), assignment_reason="especialidad fibra",
                             queued=False)  # fmt: skip

    async def mine(self, session_id):
        if not self.with_ticket:
            raise NotFoundError("no ticket for this session")
        return self._timeline("TRM-000007")

    async def timeline(self, number):
        return self._timeline(number)

    async def set_contact(self, number, value):
        self.contacts.append((number, value))


class Proactive:
    def __init__(self):
        self.jobs: list[tuple[str, dict]] = []
        self.registered: list[str] = []
        self.acks: list[str] = []
        self.hub = SimpleNamespace(register=lambda sid, send: self.registered.append(sid), unregister=lambda sid: None)

    async def schedule(self, kind, **kw):
        self.jobs.append((kind, kw))
        return "job"

    async def ack(self, notification_id):
        self.acks.append(notification_id)


def make(script, tickets=None):
    repo, kb, agent_repo, prox = FakeRepo(), FakeKnowledge(), AgentRepo(), Proactive()
    tickets = tickets or CaseTickets()
    mgr = FakeManager(script)
    tools = build_registry(
        knowledge=kb, tickets=tickets, inventory=FakeInventory(), diagnostics=FakeDiagnostics(), audit=FakeAudit()
    )
    svc = ConversationService(
        repo, mgr, tools, FakeSettings(), kb, agent=AgentService(agent_repo), tickets=tickets, proactive=prox
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_conversation_service] = lambda: svc
    c = TestClient(app)
    c.repo, c.mgr, c.agent, c.tickets, c.prox = repo, mgr, agent_repo, tickets, prox  # type: ignore[attr-defined]
    return c


def turn(ws, tid, text=None, confirm=None):
    if confirm is None:
        ws.send_json({"type": "text_message", "turn_id": tid, "text": text})
    else:
        ws.send_json({"type": "confirm_response", "turn_id": tid, "action": "ticket_create", "accepted": confirm})
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f["type"] == "turn_end":
            return frames


def start(ws):
    ws.send_json({"type": "start_session"})
    return ws.receive_json()["session_id"]


def tail(c, i=-1):
    return c.mgr.llm.chat_calls[i][-1]["content"]


def case_update(frames):
    return next(f for f in frames if f["type"] == "case_update")


def test_f0_2_asks_one_thing_accepts_h1_with_r1_r3_and_why_uses_the_rules():
    c = make(
        [
            say("¿Otros equipos tienen internet?"),
            say("¿Qué color tiene la luz?"),
            say("Parece una caída del proveedor."),
            [ChatDelta(done=True)],
        ]
    )
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        f1 = turn(ws, "t1", "Se me fue el internet")
        assert "pregunte SOLO esto: «¿Los otros equipos de la casa" in tail(c)  # F0-1: one discriminating question
        assert any(f["type"] == "progress" for f in f1) and case_update(f1)["state"] == "entendiendo"
        turn(ws, "t2", "Ningún equipo de la casa tiene internet")
        assert "luz de internet del módem" in tail(c)
        f3 = turn(ws, "t3", "El módem tiene una luz roja")
        f4 = turn(ws, "t4", "¿Por qué cree que es el proveedor?")
    cu = case_update(f3)
    assert cu["state"] == "diagnosticando" and cu["top_hypotheses"][0]["key"] == "H1_caida_proveedor"
    assert cu["top_hypotheses"][0]["cf"] >= 0.8 and cu["verified"] is False
    assert "con confianza alta" in tail(c, 2) and "registre el caso" in tail(c, 2)
    assert any(t["phase"] == "creer" and t["rules_fired"] == ["R1", "R3"] for t in c.agent.trace)
    phases = {t["phase"] for t in c.agent.trace}
    assert {"percibir", "creer", "decidir", "actuar"} <= phases
    assert [a for a in c.agent.trace if a["phase"] == "actuar"][0]["action"] == "ask_otros_equipos"
    why = "".join(f["text"] for f in f4 if f["type"] == "token")  # LLM gave nothing -> deterministic template
    assert "ningún otro equipo navega" in why and "alta" in why and "sources" not in [f["type"] for f in f4]
    assert (
        c.repo.metrics[-1]["intent"] == "why"
        and c.repo.metrics[2]["case_state"]["case"]["case_state"] == "diagnosticando"
    )


def test_f0_4_guided_step_then_user_confirms_resolution_and_followup_is_scheduled():
    c = make([say("¿Otros equipos navegan?"), say("1. Abra cmd..."), say("¡Qué bien!")])
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        turn(ws, "t1", "Mi PC tiene 169.254.10.2")
        f2 = turn(ws, "t2", "los demás sí navegan")
        f3 = turn(ws, "t3", "Listo, ya funciona")
    assert "ipconfig /release" in tail(c, 1) and case_update(f2)["state"] == "verificando"
    cu = case_update(f3)
    assert cu["state"] == "resuelto" and cu["verified"] is True and c.agent.case["verified"] is True
    assert [k for k, _ in c.prox.jobs] == ["followup"]
    assert any(t["phase"] == "verificar" and t["action"] == "verified_working" for t in c.agent.trace)


def test_ticket_followup_without_number_uses_session_ticket_template():
    c = make([say("Su caso va bien.")])
    with c.websocket_connect("/ws/session") as ws:
        sid = start(ws)
        frames = turn(ws, "t1", "¿Qué pasó con mi ticket?")
    t = tail(c)
    assert "TRM-000007" in t and "Laura Gómez (especialista en fibra)" in t and "Voy en camino" in t
    assert "5 horas" in t and "hace 2 horas" in t
    assert "tool_call" not in [f["type"] for f in frames] and c.repo.metrics[-1]["intent"] == "ticket_followup"
    assert c.prox.registered == [sid]  # WS sender registered with the NotificationHub


def test_k3_slots_confirm_request_creates_assigned_ticket_and_contact_never_persisted():
    c = make([say("Listo, TRM-000007 con Laura.")])
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        f1 = turn(ws, "t1", "Quiero que venga un técnico a revisar")
        assert "municipio" in "".join(f["text"] for f in f1 if f["type"] == "token")  # modalidad already said: visita
        turn(ws, "t2", "Puerto Asís")
        f3 = turn(ws, "t3", "en la tarde")
        assert "número o correo" in "".join(f["text"] for f in f3 if f["type"] == "token")
        f4 = turn(ws, "t4", f"mi número es {PHONE}")
        req = next(f for f in f4 if f["type"] == "confirm_request")
        assert req == {"type": "confirm_request", "turn_id": "t4", "action": "ticket_create",
                       "summary": "¿Registro el caso para una visita en Puerto Asís en la tarde?"}  # fmt: skip
        assert not c.tickets.created
        f5 = turn(ws, "t4", confirm=True)
        ws.send_json({"type": "notification_ack", "id": "n1"})
        ws.send_json({"type": "start_session"})
        while ws.receive_json()["type"] != "session":  # ack is handled before the new session frame
            pass
    kw = c.tickets.created[0]
    assert (kw["zone"], kw["service_mode"], kw["preferred_slot"]) == ("puerto_asis", "visita", "tarde")
    assert kw["handoff_summary"]["preferencias"] == {"zona": "puerto_asis", "modalidad": "visita", "franja": "tarde"}
    assert c.tickets.contacts == [("TRM-000007", PHONE)]
    assert "Laura Gómez (especialista en fibra)" in tail(c) and "en la tarde" in tail(c)
    assert case_update(f5)["state"] == "escalado" and f5[-1]["turn_id"] == "t4-confirm"
    assert sorted(k for k, _ in c.prox.jobs) == ["followup", "sla_watch"] and c.prox.acks == ["n1"]
    blob = repr(c.repo.messages) + repr(c.agent.trace) + repr(c.agent.facts) + repr(getattr(c.repo, "metrics", []))
    assert PHONE not in blob and "555 1234" not in blob and "[contacto registrado]" in repr(c.repo.messages)


def _v3(c):
    from pathlib import Path

    c.agent.rule_sets[0]["content_yaml"] = (Path(__file__).parent.parent / "rules" / "diagnostico_red.v3.yaml").read_text()


def test_v3_dns_message_accepts_h3_and_guides_without_generic_ask():
    c = make([say("Vamos con el DNS.")])
    _v3(c)
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        f = turn(ws, "t1", "Hago ping a 8.8.8.8 y responde bien, pero no se encuentra el servidor al abrir páginas")
    top = case_update(f)["top_hypotheses"][0]
    assert top["key"] == "H3_dns" and top["cf"] >= 0.8
    assert "otros equipos de la casa" not in tail(c) and "DNS" in tail(c)
    assert [a for a in c.agent.trace if a["phase"] == "actuar"][0]["action"] == "guide_cambiar_dns"


def test_consulta_intent_skips_diagnostic_questions_and_uses_rag():
    from app.conversation import policy

    assert policy.classify_intent("¿Cómo configuro una IP fija en Ubuntu con netplan?") == policy.CONSULTA
    assert policy.classify_intent("¿Qué diferencia hay entre 2.4 y 5 GHz?") == policy.CONSULTA
    assert policy.classify_intent("¿Cómo hago si no funciona el internet?") != policy.CONSULTA
    c = make([say("Edite netplan [1].")])
    _v3(c)
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        f = turn(ws, "t1", "¿Cómo configuro una IP fija en Ubuntu con netplan?")
    assert "Responda la consulta usando SOLO las fuentes" in tail(c)
    assert "otros equipos" not in tail(c) and not any(x["type"] == "ui_component" for x in f)


def test_v3_ip_conflict_accepts_h6_and_guides():
    c = make([say("Vamos a renovar la dirección.")])
    _v3(c)
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        f = turn(ws, "t1", "Windows me dice conflicto de dirección IP")
    assert case_update(f)["top_hypotheses"][0]["key"] == "H6_ip_duplicada"
    assert [a for a in c.agent.trace if a["phase"] == "actuar"][0]["action"] == "guide_ip_duplicada"
