# ruff: noqa: E501
"""In-memory fakes for conversation/WebSocket endpoint tests."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.config.models import ModelProfile
from app.conversation.ports import KnowledgeHit, TicketSummary
from app.models_runtime.manager import Lease
from app.runtimes.base import ChatDelta, RuntimeCallError, ToolCall


def _now():
    return datetime.now(UTC)


class FakeRepo:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.voice_turns: list[dict] = []

    async def create_session(self, channel):
        sid = str(uuid4())
        self.sessions[sid] = {"id": sid, "title": None, "channel": channel, "status": "active", "archived": False,
                              "started_at": _now(), "updated_at": _now()}  # fmt: skip
        return sid

    async def touch_session(self, session_id, title_if_empty):
        s = self.sessions[session_id]
        s["updated_at"], s["title"] = _now(), s["title"] or title_if_empty

    async def get_session(self, session_id):
        return next((r for r in await self.list_sessions(200, True) if r["id"] == session_id), None)

    async def update_session(self, session_id, changes):
        if session_id not in self.sessions:
            return False
        self.sessions[session_id].update(changes)
        return True

    async def delete_session(self, session_id):
        if self.sessions.pop(session_id, None) is None:
            return False
        self.messages = [m for m in self.messages if m["session_id"] != session_id]
        self.voice_turns = [t for t in self.voice_turns if t["session_id"] != session_id]
        return True

    async def session_exists(self, session_id):
        return session_id in self.sessions

    async def add_message(self, session_id, role, content, input_mode, model_profile_id):
        m = {
            "id": str(uuid4()), "session_id": session_id, "role": role, "content": content,
            "input_mode": input_mode, "model_profile_id": model_profile_id, "created_at": _now(),
        }  # fmt: skip
        self.messages.append(m)
        return m["id"]

    async def update_message_content(self, message_id, content):
        next(m for m in self.messages if m["id"] == message_id)["content"] = content

    async def recent_messages(self, session_id, limit):
        rows = [m for m in self.messages if m["session_id"] == session_id and m["role"] in ("user", "assistant")]
        return rows[-limit:]

    async def add_voice_turn(
        self, session_id, transcript, interrupted, stt_ms, rag_ms, llm_first_token_ms, tts_first_audio_ms
    ):
        t = {"id": str(uuid4()), "session_id": session_id, "transcript": transcript, "interrupted": interrupted,
             "stt_ms": stt_ms, "rag_ms": rag_ms, "tts_first_audio_ms": tts_first_audio_ms}  # fmt: skip
        self.voice_turns.append(t)
        return t["id"]

    async def add_turn_metrics(self, row):
        self.metrics = [*getattr(self, "metrics", []), row]

    async def mark_voice_turn_interrupted(self, voice_turn_id):
        next(t for t in self.voice_turns if t["id"] == voice_turn_id)["interrupted"] = True

    async def list_sessions(self, limit, include_archived=False):
        out = []
        for s in self.sessions.values():
            if s["archived"] and not include_archived:
                continue
            ms = [m for m in self.messages if m["session_id"] == s["id"] and m["role"] in ("user", "assistant")]
            out.append({**s, "message_count": len(ms), "last_message_at": ms[-1]["created_at"] if ms else None})
        return sorted(out, key=lambda r: r["updated_at"], reverse=True)[:limit]

    async def list_messages(self, session_id):
        if session_id not in self.sessions:
            return None
        return [m for m in self.messages if m["session_id"] == session_id]


class FakeSettings:
    def __init__(self, **kw):
        self.d = {"system_prompt": "Eres TRAMA.", "rag_top_k": 3, "chat_title_llm": False, **kw}  # LLM titles off: they would consume scripted replies

    async def get(self, key, default=None):
        return self.d.get(key, default)


class FakeAudit:
    def __init__(self):
        self.rows: list[tuple] = []

    async def record(self, session_id, tool_name, arguments_summary, result_summary, status):
        self.rows.append((tool_name, arguments_summary, result_summary, status))


class FakeInventory:
    async def inventory(self, site):
        return {"devices": [], "networks": []}

    async def network_details(self, name_or_cidr):
        return []


class FakeDiagnostics:
    def __init__(self):
        self.observations: list[dict] = []

    async def record(self, session_id, observations):
        self.observations += [{**o, "source": "user_provided", "session_id": session_id} for o in observations]
        return len(observations)


class FakeKnowledge:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query, top_k, filters=None, on_load=None):
        self.queries.append(query)
        return [
            KnowledgeHit(chunk_id="c1", document_id="d1", title="Guía DHCP", content="APIPA indica fallo de DHCP.",
                         source_uri="kb://dhcp", section="APIPA", score=0.91, is_synthetic=True)
        ]  # fmt: skip


class FakeTickets:
    async def create_incident_with_ticket(self, **kw):
        return TicketSummary(
            ticket_number="TRM-000001", status="open", priority=kw["priority"], title=kw["title"], created_at=_now()
        )

    async def get_ticket(self, ticket_number):
        return None


def say(*tokens: str) -> list:
    return [ChatDelta(content=t) for t in tokens] + [ChatDelta(done=True)]


def call(name: str, **args) -> list:
    return [ChatDelta(tool_calls=[ToolCall(name=name, arguments=args, id="c1")], done=True)]


def profile(role: str, params=None) -> ModelProfile:
    return ModelProfile(
        id=uuid4(), name=f"p-{role}", role=role, provider_id=uuid4(), model_ref=f"{role}-model",
        params=params or {}, est_memory_mb=1, measured_memory_mb=None, is_active=True, created_at=_now(), updated_at=_now(),
    )  # fmt: skip


class FakeAdapter:
    """`script`: one entry per chat() call; an entry is a list of ChatDelta | float (sleep) | Exception."""

    kind = "openai"

    def __init__(self, script):
        self.script = list(script)
        self.chat_calls: list[list[dict]] = []
        self.tool_names: list[list[str]] = []  # tools offered on each chat() call
        self.transcript = "mi pc no tiene internet"

    async def chat(self, model, messages, tools=None, stream=True, params=None):
        self.chat_calls.append([dict(m) for m in messages])
        self.tool_names.append([t["function"]["name"] for t in tools or []])
        entry = self.script.pop(0) if self.script else say("ok")
        for item in entry:
            if isinstance(item, Exception):
                raise item
            if isinstance(item, float):
                await asyncio.sleep(item)
            else:
                yield item

    async def transcribe(self, model, audio, mime, language=None, params=None):
        assert language == "es"
        return self.transcript

    async def synthesize(self, model, text, voice=None, format="wav", params=None):
        return b"RIFF" + text.encode()


class FakeManager:
    def __init__(self, llm_script):
        self.llm = FakeAdapter(llm_script)
        self.profiles = {"llm": profile("llm"), "stt": profile("stt", {"language": "es"}), "tts": profile("tts")}

    @asynccontextmanager
    async def use(self, role, on_load=None):
        yield Lease(self.llm, self.profiles[role])


__all__ = ["RuntimeCallError", "UUID"]
