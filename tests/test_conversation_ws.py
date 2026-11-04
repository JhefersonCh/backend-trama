# ruff: noqa: E501
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config.errors import NotFoundError
from app.conversation.deps import get_conversation_service
from app.conversation.router import router
from app.conversation.service import ConversationService
from app.runtimes.base import RuntimeCallError
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
    call,
    say,
)

IPCONFIG_APIPA = """
Windows IP Configuration

Ethernet adapter Ethernet:

   Connection-specific DNS Suffix  . :
   Description . . . . . . . . . . . : Intel(R) Ethernet
   DHCP Enabled. . . . . . . . . . . : Yes
   Autoconfiguration IPv4 Address. . : 169.254.23.45(Preferred)
   Subnet Mask . . . . . . . . . . . : 255.255.0.0
   Default Gateway . . . . . . . . . :
"""


def make(script, **settings):
    repo, audit, diag, kb = FakeRepo(), FakeAudit(), FakeDiagnostics(), FakeKnowledge()
    mgr = FakeManager(script)
    tools = build_registry(
        knowledge=kb, tickets=FakeTickets(), inventory=FakeInventory(), diagnostics=diag, audit=audit
    )
    svc = ConversationService(repo, mgr, tools, FakeSettings(**settings), kb)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_conversation_service] = lambda: svc

    @app.exception_handler(NotFoundError)
    async def _nf(_, e):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(e)}, status_code=404)

    c = TestClient(app)
    c.repo, c.audit, c.diag, c.mgr = repo, audit, diag, mgr  # type: ignore[attr-defined]
    return c


def collect(ws, until="turn_end"):
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f["type"] == until:
            return frames


def start(ws, voice=False):
    ws.send_json({"type": "start_session", "voice": voice})
    f = ws.receive_json()
    assert f["type"] == "session"
    return f["session_id"]


def types(frames):
    return [f["type"] for f in frames]


def test_text_turn_streams_tokens_and_turn_end():
    c = make([say("Hola, ", "¿qué ", "pasa?")])
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "hola"})
        frames = collect(ws)
    assert "".join(f["text"] for f in frames if f["type"] == "token") == "Hola, ¿qué pasa?"
    end = frames[-1]
    assert end["interrupted"] is False and end["metrics"]["total_ms"] >= 0
    assert end["metrics"]["llm_first_token_ms"] is not None and end["metrics"]["stt_ms"] is None
    assert [m["role"] for m in c.repo.messages] == ["user", "assistant"]  # smalltalk: no RAG, no sources
    assert c.repo.messages[-1]["model_profile_id"] == c.mgr.profiles["llm"].id
    system = c.mgr.llm.chat_calls[0][0]
    assert system["role"] == "system" and "Español" in system["content"]


def test_tool_roundtrip_emits_call_result_sources_and_audits():
    c = make([call("search_knowledge", query="apipa"), say("Según la Guía DHCP, es APIPA.")], rag_mode="tool")
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "tengo 169.254.1.1"})
        frames = collect(ws)
    t = types(frames)
    assert t.index("tool_call") < t.index("tool_result") < t.index("sources") < t.index("token")
    assert {"type": "state", "turn_id": "t1", "state": "retrieving", "detail": "search_knowledge"} in frames
    src = next(f for f in frames if f["type"] == "sources")["items"][0]
    assert src == {
        "title": "Guía DHCP",
        "source_uri": "kb://dhcp",
        "section": "APIPA",
        "score": 0.91,
        "is_synthetic": True,
        "document_id": "d1",
        "chunk_id": "c1",
        "chunk_index": None,
        "excerpt": "APIPA indica fallo de DHCP.",
    }
    assert frames[-1]["metrics"]["rag_ms"] is not None
    assert c.audit.rows[0][0] == "search_knowledge" and c.audit.rows[0][3] == "ok"
    # tool result was fed back to the LLM on the 2nd call
    assert c.mgr.llm.chat_calls[1][-1]["role"] == "tool"


def test_audio_turn_transcribes_then_streams_and_speaks_sentence_by_sentence():
    c = make([say("Tu IP es 192.168.1.1. ", "Reinicia el router, Sr. Pérez. ", "Avísame.")])
    audio = base64.b64encode(b"RIFFfake").decode()
    with c.websocket_connect("/ws/session") as ws:
        start(ws, voice=True)
        ws.send_json({"type": "audio_segment", "turn_id": "t1", "mime": "audio/wav", "data": audio})
        frames = collect(ws)
    t = types(frames)
    assert (
        t.index("transcript") < t.index("token") and frames[t.index("transcript")]["text"] == "mi pc no tiene internet"
    )
    chunks = [f for f in frames if f["type"] == "audio_chunk"]
    assert [x["seq"] for x in chunks] == [0, 1, 2]
    assert [x["text"] for x in chunks] == ["Tu IP es 192.168.1.1.", "Reinicia el router, Sr. Pérez.", "Avísame."]
    assert base64.b64decode(chunks[0]["data"]).startswith(b"RIFF")
    m = frames[-1]["metrics"]
    assert m["stt_ms"] is not None and m["tts_first_audio_ms"] is not None
    assert c.repo.voice_turns[0]["transcript"] == "mi pc no tiene internet" and not c.repo.voice_turns[0]["interrupted"]
    assert c.repo.messages[0]["input_mode"] == "voice"


def test_cancel_interrupts_turn_and_persists_only_played_text():
    c = make([say("Primera frase. ") + [5.0] + say("nunca llega")])
    with c.websocket_connect("/ws/session") as ws:
        start(ws, voice=True)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "explícame"})
        while ws.receive_json()["type"] != "audio_chunk":
            pass
        ws.send_json({"type": "playback_progress", "turn_id": "t1", "played_seq": 0})
        ws.send_json({"type": "cancel", "turn_id": "t1"})
        frames = collect(ws)
    assert frames[-1]["interrupted"] is True or any(f.get("interrupted") for f in frames if f["type"] == "turn_end")
    assert c.repo.voice_turns[0]["interrupted"] is True
    assert c.repo.messages[-1] == {**c.repo.messages[-1], "role": "assistant", "content": "Primera frase."}


def test_auto_rag_injects_context_and_emits_sources_without_tool_call():
    c = make([say("Es APIPA [1].")], rag_top_k=3, rag_min_score=0.5)
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "mi pc tiene 169.254.33.10"})
        frames = collect(ws)
    t = types(frames)
    assert "tool_call" not in t and t.index("sources") < t.index("token")
    assert {"type": "state", "turn_id": "t1", "state": "retrieving"} in frames
    assert "loading_model" not in [f.get("state") for f in frames]
    src = next(f for f in frames if f["type"] == "sources")["items"]
    assert src == [
        {
            "title": "Guía DHCP",
            "source_uri": "kb://dhcp",
            "section": "APIPA",
            "score": 0.91,
            "is_synthetic": True,
            "document_id": "d1",
            "chunk_id": "c1",
            "chunk_index": None,
            "excerpt": "APIPA indica fallo de DHCP.",
        }
    ]
    assert frames[-1]["metrics"]["rag_ms"] is not None
    *_, user, tail = c.mgr.llm.chat_calls[0]
    assert user == {"role": "user", "content": "mi pc tiene 169.254.33.10"}  # history/user text stay byte-stable
    assert (
        tail["role"] == "user"
        and "[1] Guía DHCP — APIPA (sintético)" in tail["content"]
        and "INSTRUCCIÓN" in tail["content"]
    )
    assert "search_knowledge" not in c.mgr.llm.tool_names[0] and "get_ticket_status" in c.mgr.llm.tool_names[0]


def test_citation_validation_strips_bad_markers_and_turn_end_carries_final_text():
    c = make(
        [
            say(
                "Hecho: su equipo recibió una dirección APIPA de DHCP [1]. Reinícielo [3]. La luz roja es del módem [1]."
            )
        ],
        rag_top_k=3,
        rag_min_score=0.5,
    )
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "mi pc tiene 169.254.33.10"})
        frames = collect(ws)
    final = frames[-1]["final_text"]
    assert final == "su equipo recibió una dirección APIPA de DHCP [1]. Reinícielo. La luz roja es del módem."
    assert c.repo.messages[-1]["content"] == final
    assert "Hecho:" in "".join(
        f["text"] for f in frames if f["type"] == "token"
    )  # streamed raw, replaced by final_text


def test_policy_placeholders_clarify_guide_step_and_physical_retrieval():
    from app.conversation import policy

    assert (
        policy.clean_answer(
            "Sigue viendo luz roja, [n] le recomiendo [x] llamar [] al proveedor [5].", ["a"], "", False
        )
        == "Sigue viendo luz roja, le recomiendo llamar al proveedor."
    )
    assert "ticket" not in policy.clean_answer("Hola. ¿Quiere que registre un ticket?", [], "", False, policy.SMALLTALK)
    assert policy.clean_answer("Pierde la conexión.\n\n1. Reinicie.", [], "", False) == "1. Reinicie."
    assert policy.classify_intent("¿Y eso de la luz roja qué significa exactamente?") == policy.CLARIFY
    assert policy.last_step("Haga esto:\n1. Abra cmd\n2. Escriba **ipconfig**\n¿Pudo?") == "Escriba ipconfig"
    assert "ipconfig" in policy.instruction(policy.GUIDE, "basico", False, False, "usted", "Escriba ipconfig")
    assert (
        policy.max_tokens_for(policy.DIAGNOSTIC, False) == 1100 and policy.max_tokens_for(policy.CLARIFY, False) == 500
    )
    assert policy.clean_answer("1. Uno.\n2. Dos.\n4.\n-", [], "", False) == "1. Uno.\n2. Dos."
    assert policy.clean_answer("1. Ping a 169.254.1.1 ahora.\n2. Dos.", [], "", False) == "1. Dos."
    t = ["Es que se me fue el wifi", "El modem tiene una luz roja", "Ya lo reinicié y la luz roja sigue igual"]
    for i, off in ((1, [False]), (2, [False, True])):
        assert policy.ticket_decision(t[i], policy.case_snapshot(t[: i + 1]), off, True) is True
    snap = policy.case_snapshot(t)
    assert snap["steps_tried"] == ["reinició el módem"]
    assert "YA hizo" in policy.instruction(policy.DIAGNOSTIC, "basico", False, True, snap=snap)
    assert "ipconfig /release" in policy.instruction(policy.DIAGNOSTIC, "basico", False, True, apipa=True)
    q = "No, no he cambiado la clave, el modem tiene una luz roja"
    assert "clave" not in policy.expand_query(q) and not policy.password_mentioned(q)
    titles = ["NET-010 La red Wi-Fi rechaza la autenticación", "Luces del módem"]
    assert policy.outage_boost(q, titles) == [1]


def test_intent_router_offers_no_ticket_tools_for_diagnostic_and_smalltalk_skips_rag():
    from app.conversation import policy

    names = ["search_knowledge", "get_ticket_status", "create_ticket", "get_network_inventory", "parse_command_output"]
    msg = "Mi computador está conectado al Wi-Fi pero no navega"
    assert policy.classify_intent(msg) == policy.DIAGNOSTIC
    assert "get_ticket_status" in policy.tool_exclusions(policy.DIAGNOSTIC, names, msg)
    assert policy.classify_intent("¿En qué estado está el ticket TRM-000001?") == policy.TICKET
    assert (
        policy.classify_intent("Hola") == policy.SMALLTALK
        and policy.classify_intent("No sé cómo hacer eso") == policy.GUIDE
    )


def test_new_message_barges_in_and_runtime_error_does_not_kill_socket():
    c = make([say("a") + [5.0], [RuntimeCallError("llm caído")], say("de vuelta")])
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "uno"})
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "text_message", "turn_id": "t2", "text": "dos"})
        frames = collect(ws, "turn_end")
        first_end = next(f for f in frames if f["type"] == "turn_end")
        assert first_end["turn_id"] == "t1" and first_end["interrupted"] is True
        rest = frames if first_end is frames[-1] else frames
        if first_end is frames[-1]:
            rest = collect(ws, "turn_end")
        assert any(f["type"] == "error" and "llm caído" in f["detail"] for f in rest)
        ws.send_json({"type": "text_message", "turn_id": "t3", "text": "tres"})
        out = collect(ws)
        assert "de vuelta" in "".join(f.get("text", "") for f in out if f["type"] == "token")


def test_history_endpoints_return_messages_with_sources():
    c = make([call("search_knowledge", query="dhcp"), say("Respuesta citada.")])
    with c.websocket_connect("/ws/session") as ws:
        sid = start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "pregunta"})
        collect(ws)
    sessions = c.get("/api/sessions").json()
    assert sessions[0]["id"] == sid and sessions[0]["message_count"] == 2
    msgs = c.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "Respuesta citada." and msgs[1]["sources"][0]["title"] == "Guía DHCP"
    assert c.get("/api/sessions/00000000-0000-4000-8000-000000000000/messages").status_code == 404


def test_session_crud_auto_title_and_resume():
    c = make([say("ok")])
    new = c.post("/api/sessions")
    assert new.status_code == 201 and new.json()["title"] is None and new.json()["message_count"] == 0
    sid = new.json()["id"]
    with c.websocket_connect("/ws/session") as ws:
        ws.send_json({"type": "start_session", "session_id": sid})
        assert ws.receive_json() == {"type": "session", "session_id": sid}  # resumed, not a new chat
        ws.send_json(
            {
                "type": "text_message",
                "turn_id": "t1",
                "text": "Mi computador está conectado al Wi-Fi pero no navega desde esta mañana",
            }
        )
        collect(ws)
    row = c.get("/api/sessions").json()[0]
    assert row["id"] == sid and row["title"] == "Mi computador está conectado al Wi-Fi pero no navega desde…"
    r = c.patch(f"/api/sessions/{sid}", json={"title": "Wi-Fi sala 2", "archived": True})
    assert r.status_code == 200 and r.json()["title"] == "Wi-Fi sala 2" and r.json()["archived"] is True
    assert c.get("/api/sessions").json() == [] and len(c.get("/api/sessions?include_archived=true").json()) == 1
    assert c.delete(f"/api/sessions/{sid}").status_code == 204
    assert c.delete(f"/api/sessions/{sid}").status_code == 404 and c.repo.messages == []


def test_parse_command_output_detects_apipa_and_stores_observation():
    c = make(
        [
            call("parse_command_output", os="windows", command="ipconfig /all", raw_output=IPCONFIG_APIPA),
            say("Es APIPA."),
        ]
    )
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "pego ipconfig"})
        frames = collect(ws)
    res = next(f for f in frames if f["type"] == "tool_result")
    assert res["ok"] is True and "APIPA" in res["summary"]
    obs = c.diag.observations[0]
    assert (
        obs["source"] == "user_provided" and obs["result"]["apipa"] is True and obs["result"]["ipv4"] == "169.254.23.45"
    )
    assert "apipa" in c.mgr.llm.chat_calls[1][-1]["content"]


def test_create_ticket_and_invalid_tool_args_are_reported_not_fatal():
    c = make(
        [
            call(
                "create_ticket",
                title="Sin red",
                description="PC sin DHCP",
                category="dhcp",
                priority="high",
                evidence=["APIPA"],
            ),
            call("create_ticket", title="x", category="nope"),
            say("listo"),
        ]
    )
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "mi pc no tiene red"})
        frames = collect(ws)
    results = [f for f in frames if f["type"] == "tool_result"]
    assert results[0]["ok"] is True and "TRM-000001" in results[0]["summary"]
    assert results[1]["ok"] is False and "inválidos" in results[1]["summary"]
    assert [r[3] for r in c.audit.rows] == ["ok", "error"]


@pytest.mark.parametrize("frame", [{"type": "nope"}, {"type": "text_message", "turn_id": "t"}])
def test_invalid_frame_returns_error_event(frame):
    c = make([])
    with c.websocket_connect("/ws/session") as ws:
        ws.send_json(frame)
        assert ws.receive_json()["type"] == "error"


def test_affirmative_after_offer_creates_ticket_in_backend_once_and_stops_offering():
    from app.conversation import policy

    offer = "¿Quiere que registre el problema para que un técnico lo revise?"
    assert policy.classify_intent("Sí, me gustaría que entedipo lo que dice por favor.", offer) == policy.TICKET_CREATE
    assert policy.classify_intent("un técnico de la previsión") == policy.TICKET_CREATE
    assert policy.classify_intent("No, gracias", offer) != policy.TICKET_CREATE
    assert policy.classify_intent("Sí, me gustaría") != policy.TICKET_CREATE  # no offer before
    c = make([say(offer), say("Listo, ticket TRM-000001."), say("Ya existe TRM-000001.")])
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json(
            {"type": "text_message", "turn_id": "t1", "text": "el módem tiene luz roja, ya lo reinicié y sigue"}
        )
        collect(ws)
        ws.send_json({"type": "text_message", "turn_id": "t2", "text": "Sí por favor"})
        f2 = collect(ws)
        ws.send_json({"type": "text_message", "turn_id": "t3", "text": "dale"})
        f3 = collect(ws)
    r = [f for f in f2 if f["type"] == "tool_result"]
    assert [f["name"] for f in f2 if f["type"] == "tool_call"] == ["create_ticket"] and "TRM-000001" in r[0]["summary"]
    assert [f["type"] for f in f3].count("tool_call") == 0  # idempotent per session
    assert [r[3] for r in c.audit.rows] == ["ok"]
    assert "TRM-000001" in c.mgr.llm.chat_calls[1][-1]["content"] and "high" in c.mgr.llm.chat_calls[1][-1]["content"]
    assert f2[-1]["type"] == "turn_end"


def test_page_context_is_accepted_and_hint_reaches_the_turn():
    c = make([say("Listo.")])
    with c.websocket_connect("/ws/session") as ws:
        start(ws)
        ws.send_json({"type": "text_message", "turn_id": "t1", "text": "¿qué pasa con este ticket?",
                      "context": {"route": "/tickets", "entity": {"type": "ticket", "id": "TRM-000008"}}})
        frames = collect(ws)
    assert frames[-1]["type"] == "turn_end"
    assert any("el ticket TRM-000008" in m["content"] for m in c.mgr.llm.chat_calls[0] if m["role"] == "user")
