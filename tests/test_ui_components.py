# ruff: noqa: E501
"""Generative UI (U1+U2): ui_component frames, ui_event -> facts, history. Fakes only (rules v2, no live LLM)."""

from datetime import UTC, datetime
from pathlib import Path

from app.ui.builders import topology_from_facts
from tests import test_conversation_agent as base

V2 = (Path(__file__).parent.parent / "rules" / "diagnostico_red.v2.yaml").read_text()
PHONE = "310 555 1234"


class V2Repo(base.AgentRepo):
    def __init__(self):
        super().__init__()
        self.rule_sets = [{"version": 2, "name": "diagnostico_red", "content_yaml": V2, "is_active": True,
                           "created_at": datetime.now(UTC)}]  # fmt: skip


def make():
    c = base.make([])
    c.agent.rule_sets = V2Repo().rule_sets  # same fake repo object the service uses, v2 rules active
    return c


def ui(frames, component=None):
    out = [f for f in frames if f["type"] == "ui_component"]
    return [f for f in out if f["component"] == component] if component else out


def event(ws, tid, comp, action, payload, cid=""):
    ws.send_json(
        {
            "type": "ui_event",
            "turn_id": tid,
            "component_id": cid,
            "component": comp,
            "action": action,
            "payload": payload,
        }
    )
    frames = []
    while (f := ws.receive_json())["type"] != "turn_end":
        frames.append(f)
    return frames


def test_quick_reply_sets_fact_fires_r1_then_why_shows_bars_and_flow_and_history_keeps_ui():
    c = make()
    with c.websocket_connect("/ws/session") as ws:
        sid = base.start(ws)
        f1 = base.turn(ws, "t1", "Se me fue el internet")
        qr = ui(f1, "quick_replies")[0]
        assert qr["props"]["fact"] == "otros_equipos_navegan" and [o["label"] for o in qr["props"]["options"]] == [
            "Sí",
            "No",
            "No sé",
        ]
        assert "NO enumere las opciones" in base.tail(c) and qr["turn_id"] == "t1" and qr["fallback_text"]
        f2 = event(
            ws, "t2", "quick_replies", "quick_reply", {"fact": "otros_equipos_navegan", "value": False}, qr["id"]
        )
        assert any(t["phase"] == "creer" and t["rules_fired"] == ["R1"] for t in c.agent.trace)
        assert any(t["phase"] == "percibir" and t["reason"] == "interacción con componente" for t in c.agent.trace)
        fact = next(f for f in c.agent.facts if f["key"] == "otros_equipos_navegan" and not f["superseded"])
        assert (fact["value"], fact["certainty"], fact["source"]) == (False, 1.0, "user_reported")
        luz = ui(f2, "quick_replies")[0]
        assert luz["props"]["fact"] == "luz_modem" and "Roja" in [o["label"] for o in luz["props"]["options"]]
        f3 = event(ws, "t3", "quick_replies", "quick_reply", {"fact": "luz_modem", "value": "roja"}, luz["id"])
        assert {"case_card", "hypothesis_bars"} <= {x["component"] for x in ui(f3)}  # H1 accepted
        assert ui(f3, "case_card")[0]["props"]["top_hypothesis"]["confidence_label"] == "muy probable"
        f4 = base.turn(ws, "t4", "¿Por qué cree que es el proveedor?")
    bars, flow = ui(f4, "hypothesis_bars")[0], ui(f4, "diagnosis_flow")[0]
    assert bars["props"]["items"][0]["key"] == "H1_caida_proveedor" and bars["props"]["thresholds"]["accept"] == 0.8
    kinds = {n["kind"] for n in flow["props"]["nodes"]}
    assert {"question", "answer", "rule", "hypothesis"} <= kinds
    assert {n["id"] for n in flow["props"]["nodes"] if n["kind"] == "rule"} == {"r_R1", "r_R3"}
    hist = c.get(f"/api/sessions/{sid}/messages").json()
    assert [m["content"] for m in hist if m["role"] == "user"][1:3] == ["No", "Roja"]
    ui_rows = [m for m in hist if m["role"] == "ui"]
    assert ui_rows[0]["id"] == qr["id"] and ui_rows[0]["component"] == "quick_replies" and ui_rows[0]["props"]["fact"]
    assert "diagnosis_flow" in [m["component"] for m in ui_rows] and len({m["id"] for m in ui_rows}) == len(ui_rows)


def test_checklist_steps_update_in_place_and_completion_asks_verification_with_light_chips():
    c = make()
    with c.websocket_connect("/ws/session") as ws:
        sid = base.start(ws)
        base.turn(ws, "t1", "Se me fue el internet")
        f2 = event(ws, "t2", "compare_cards", "compare_choose", {"option_id": "guide_reinicio"})
        ck = ui(f2, "checklist")[0]
        assert (
            ck["props"]["current"] == "p1"
            and len(ck["props"]["steps"]) == 3
            and "NO enumere ni repita los pasos" in base.tail(c)
        )
        f3 = event(ws, "t3", "checklist", "step_done", {"action_id": "guide_reinicio", "step_id": "p1"}, ck["id"])
        assert ui(f3, "checklist")[0]["id"] == ck["id"] and ui(f3, "checklist")[0]["props"]["current"] == "p2"
        assert "Paso 2 de 3" in "".join(f["text"] for f in f3 if f["type"] == "token")
        event(ws, "t4", "checklist", "step_done", {"action_id": "guide_reinicio", "step_id": "p2"}, ck["id"])
        f5 = event(ws, "t5", "checklist", "step_done", {"action_id": "guide_reinicio", "step_id": "p3"}, ck["id"])
    assert ui(f5, "checklist")[0]["props"]["current"] is None
    assert any(f["key"] == "reinicio_hecho" and f["value"] is True for f in c.agent.facts)
    verify = ui(f5, "quick_replies")[0]
    assert verify["props"]["fact"] == "luz_tras_reinicio" and "Sigue con falla" in [
        o["label"] for o in verify["props"]["options"]
    ]
    hist = [
        m for m in c.get(f"/api/sessions/{sid}/messages").json() if m["role"] == "ui" and m["component"] == "checklist"
    ]
    assert len(hist) == 1 and all(s["status"] == "done" for s in hist[0]["props"]["steps"])  # same id = one entry


def test_form_submit_goes_to_confirm_request_and_contact_is_never_persisted():
    c = make()
    with c.websocket_connect("/ws/session") as ws:
        base.start(ws)
        f1 = base.turn(ws, "t1", "Necesito que me registren el caso con un técnico")
        form = ui(f1, "form")[0]
        assert {f["name"] for f in form["props"]["fields"]} >= {"zona", "franja", "contacto"}
        assert {"label": "Puerto Asís", "value": "puerto_asis"} in form["props"]["fields"][0]["options"]
        values = {"zona": "puerto_asis", "modalidad": "visita", "franja": "tarde", "contacto": PHONE}
        f2 = event(ws, "t2", "form", "form_submit", {"form_id": "assignment", "values": values}, form["id"])
        req = next(f for f in f2 if f["type"] == "confirm_request")
        assert req["summary"] == "¿Registro el caso para una visita en Puerto Asís en la tarde?"
        base.turn(ws, "t2", confirm=True)
    assert c.tickets.contacts == [("TRM-000007", PHONE)]
    blob = repr(c.repo.messages) + repr(c.agent.trace) + repr(c.agent.facts)
    assert PHONE not in blob and "con contacto" in repr(c.repo.messages)
    comps = [m["content"] for m in c.repo.messages if m["role"] == "ui"]
    assert any('"case_card"' in x and "TRM-000007" in x for x in comps) and any('"report_card"' in x for x in comps)


def test_topology_statuses_from_facts():
    red = {
        n["id"]: n for n in topology_from_facts({"luz_modem": "roja", "otros_equipos_navegan": False})["props"]["nodes"]
    }
    assert (red["modem"]["status"], red["devices"]["status"], red["isp"]["status"]) == ("fail", "fail", "unknown")
    assert "Sin confirmar" in red["isp"]["detail"]
    pc = topology_from_facts({"otros_equipos_navegan": True, "ip_apipa": True})
    st = {n["id"]: n["status"] for n in pc["props"]["nodes"]}
    assert st == {"isp": "ok", "modem": "ok", "router": "ok", "devices": "ok", "pc": "fail"}
    assert {(e["from"], e["to"]): e["status"] for e in pc["props"]["edges"]}[("router", "pc")] == "fail"
    assert "169.254" in pc["spoken"] and "from" in pc["props"]["edges"][0]


def test_green_lights_phone_and_topology_kind():
    from app.conversation import agent_policy as ap
    from app.ui import builders as ub

    out = dict(ap.extract_facts("Lo hice y toda la luz está prendida con verde", "guide_reinicio", {}))
    assert out["luz_modem"] == "verde" and out["luz_tras_reinicio"] == "normal"
    out2 = dict(ap.extract_facts("todas prendidas en verde", None, {}, restart_guided=True))
    assert out2["luz_tras_reinicio"] == "normal"
    assert ap.extract_device("Solo en mi celular no tengo internet. Todos los otros dispositivos sí tienen internet") == "celular"
    assert ap.REDO_DIAGRAM.search("¿Puedes hacer el diagrama de red otra vez (pero bien)?")
    topo = ub.topology_from_facts({"otros_equipos_navegan": True, "equipo_afectado": "celular"})
    pc = next(n for n in topo["props"]["nodes"] if n["id"] == "pc")
    assert pc["kind"] == "device" and pc["label"] == "Su celular" and pc["status"] == "warn"
