# ruff: noqa: E501
"""Conversation orchestrator: one turn = (STT) -> history -> LLM tool loop -> (sentence-wise TTS) -> persistence."""

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.config.errors import ConflictError, NotFoundError
from app.knowledge.chunking import excerpt
from app.models_runtime.errors import RuntimeUnavailableError
from app.models_runtime.manager import Lease
from app.runtimes.base import RuntimeCallError, ToolCall
from app.tools.registry import ToolContext, ToolRegistry

from . import agent_policy, policy
from .agent_turn import PROGRESS, AgentPlan, CaseAgent
from .models import MessageOut, SessionOut, as_source
from .ports import (
    CaseTicketPort,
    ConversationRepo,
    KnowledgeHit,
    KnowledgeSearchPort,
    NoopProactive,
    ProactivePort,
    SettingsReader,
)
from .prompts import (
    CONTEXT_HEADER,
    DEFAULT_SYSTEM_PROMPT,
    EMPTY_ANSWER_FALLBACK,
    NO_CONTEXT,
    RULES_ADDENDUM,
    TOOL_RAG_RULE,
    TRUNCATED_SUFFIX,
)
from .splitter import SentenceSplitter, clean_for_speech

log = logging.getLogger("trama.conversation")

MAX_TOOL_ITERATIONS = 4
RAG_MODES = ("auto", "tool", "both")
DEFAULT_MIN_SCORE = 0.7  # seed corpus: noise floor 0.60-0.66, relevant hits 0.71-0.80
RAG_MAX_HITS = 3  # injected fragments (keeps the per-turn tail < ~500 tokens, see below)
RAG_REL_MARGIN = 0.04  # also drop hits far below the best one (keeps the injected context focused)
DEFAULT_MAX_TOKENS = 400  # voice/short answers; text diagnostic turns get more (policy.max_tokens_for)
CONDITIONAL_TOOLS = False  # offer only the tools the intent needs (changes the prompt prefix -> see docs)
HISTORY_MAX_CHARS = 4500  # ~1100 tokens of prior turns at most; the oldest WHOLE messages are dropped first
HISTORY_MSG_CHARS = 1000  # explanatory answers carry numbered steps: do not cut them to a stub
CTX_HIT_CHARS = 420  # per injected fragment
# Whole injected context (~350 tokens). llama.cpp keeps a cache checkpoint ~516 tokens before the end of each prompt
# (LFM3 is hybrid); a per-turn tail (context + question) shorter than that lets the next turn restore
# system + tools + history from cache instead of re-reading everything.
CTX_MAX_CHARS = 1300
SHORT_QUERY_WORDS = 10  # below this an elliptical follow-up is enriched with the previous user message
TITLE_CHARS = 60
_TICKET_RE = re.compile(r"\bTRM-\d+", re.IGNORECASE)
_CITE_RE = re.compile(r"\[\d+\]")
Emit = Callable[[dict[str, Any]], Awaitable[None]]
_EXPECTED_ERRORS = (RuntimeCallError, RuntimeUnavailableError, ConflictError, NotFoundError)


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def _uuid(v: str) -> bool:
    try:
        UUID(v)
    except ValueError:
        return False
    return True


def make_title(text: str, n: int = TITLE_CHARS) -> str:
    """Chat title from the first user message: whitespace-collapsed, cut at a word boundary (no LLM call)."""
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    head = t[: n + 1]
    cut = head.rsplit(" ", 1)[0] if " " in head else t[:n]
    return cut.rstrip(" ,.;:¿?¡!") + "…"


def _source(h: KnowledgeHit) -> dict[str, Any]:
    return {
        "title": h.title,
        "source_uri": h.source_uri,
        "section": h.section,
        "score": round(h.score, 4),
        "is_synthetic": h.is_synthetic,
        "document_id": h.document_id,
        "chunk_id": h.chunk_id,
        "chunk_index": getattr(h, "chunk_index", None),
        "excerpt": excerpt(h.content),
    }


def _clip(text: str, n: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def context_block(hits: list[KnowledgeHit]) -> str:
    """Compact numbered context: `[1] Título — sección (sintético)` + a clipped excerpt."""
    if not hits:
        return NO_CONTEXT
    lines, used = [CONTEXT_HEADER], 0
    for i, h in enumerate(hits, 1):
        label = (
            f"[{i}] {h.title}" + (f" — {h.section}" if h.section else "") + (" (sintético)" if h.is_synthetic else "")
        )
        body = _clip(h.content, min(CTX_HIT_CHARS, max(120, CTX_MAX_CHARS - used)))
        used += len(body)
        lines.append(f"{label}\n{body}")
        if used >= CTX_MAX_CHARS:
            break
    return "\n".join(lines)


def _clip_sentences(text: str, n: int) -> str:
    """Clip at a sentence end (no ellipsis: the model copies dangling fragments from its own history)."""
    if len(text) <= n:
        return text
    head = text[:n]
    cut = max(head.rfind(". "), head.rfind("? "), head.rfind("! "), head.rfind("\n"))
    return head[: cut + 1].rstrip() if cut > n // 3 else head.rsplit(" ", 1)[0]


def trim_history(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Newest turns that fit HISTORY_MAX_CHARS (each message clipped), oldest first, starting with a user turn."""
    out: list[dict[str, str]] = []
    total = 0
    for m in reversed(rows):
        c = _clip_sentences(m["content"], HISTORY_MSG_CHARS)
        if total + len(c) > HISTORY_MAX_CHARS:
            break
        out.append({"role": m["role"], "content": c})
        total += len(c)
    out.reverse()
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


_ENTITY_ES = {"ticket": "el ticket", "case": "el caso", "technician": "el técnico"}


def page_hint(context: dict | None) -> str:
    """Short hint so 'este ticket' resolves. Only route + entity id (no contact data)."""
    if not context:
        return ""
    ent = context.get("entity")
    if ent and ent.get("type") in _ENTITY_ES:
        return f"Contexto de pantalla: el usuario está viendo {_ENTITY_ES[ent['type']]} {ent['id']}."
    route = context.get("route")
    return f"Contexto de pantalla: el usuario está en la sección {route}." if route else ""


@dataclass
class TurnRequest:
    text: str | None = None
    audio: bytes | None = None
    mime: str = "audio/wav"
    confirm: bool | None = None  # confirm_response frame (accepted) -> answers the pending confirm_request
    ui_event: dict | None = None  # ui_event frame {component_id, component, action, payload}
    media_id: str | None = None  # media_message frame (text = what the person wrote with it)
    context: dict | None = None  # text_message `context` {route, entity?{type,id}}: page the person is viewing


@dataclass
class TurnState:
    turn_id: str
    session_id: str
    voice_out: bool
    emit: Emit
    input_mode: str = "text"
    assistant_text: str = ""
    chunks: dict[int, str] = field(default_factory=dict)  # seq -> sentence actually sent as audio
    played_seq: int | None = None
    interrupted: bool = False
    ended: bool = False
    user_text: str = ""
    stt_ms: int | None = None
    rag_ms: int | None = None
    first_token_ms: int | None = None
    first_audio_ms: int | None = None
    t_start: float = field(default_factory=time.perf_counter)
    t_llm: float | None = None
    llm_profile_id: UUID | None = None
    stt_profile_id: UUID | None = None
    assistant_message_id: str | None = None
    voice_turn_id: str | None = None
    errored: bool = False
    streamed: str = ""  # everything streamed to the client (raw)
    source_texts: list[str] = field(default_factory=list)
    max_tokens: int | None = None
    token_limits: dict | None = None  # `max_tokens_by_intent` setting, read once per turn
    title_seed: tuple[str, str] | None = None  # (first user message, fallback title) -> LLM title after the turn
    intent: str | None = None
    audience: str | None = None
    case_snapshot: dict | None = None
    ticket_offer: bool | None = None
    sources_count: int = 0
    top_source_score: float | None = None
    tool_calls: int = 0
    tool_errors: int = 0
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    citations_removed: int = 0
    rewritten: bool = False
    confirm: bool | None = None
    progress_on: bool = False  # agent mode: emit I1 `progress` frames (max one per stage)
    progress_sent: set[str] = field(default_factory=set)
    # generative UI (set by CaseAgent.perceive_ui / perceive_media, consumed by CaseAgent.before)
    ui_event: dict | None = None
    ui_intent: str | None = None
    ui_facts: list[dict] = field(default_factory=list)  # facts already stored from the interaction
    ui_fixed: str | None = None  # deterministic reply (e.g. next checklist step), no LLM
    ui_force: str | None = None  # action chosen by the person (topology node, compare card, discarded photo)
    ui_verify: str | None = None  # guided action whose checklist was completed -> verification
    ui_step_failed: str | None = None  # text of a checklist step the person could not do
    ui_target: str | None = None  # hypothesis asked about from hypothesis_bars
    ui_note: str = ""  # extra instruction for the LLM (e.g. photo could not be analyzed)
    ui_pending: list[dict] = field(default_factory=list)  # components produced while perceiving
    ui_emitted: list[dict] = field(default_factory=list)  # persisted as role 'ui' rows in finish()

    def add_usage(self, u: dict[str, int] | None) -> None:
        if not u:
            return
        self.prompt_tokens = (self.prompt_tokens or 0) + u.get("prompt_tokens", 0)
        self.cached_tokens = (self.cached_tokens or 0) + u.get("cached_tokens", 0)
        self.completion_tokens = (self.completion_tokens or 0) + u.get("completion_tokens", 0)

    @property
    def voice(self) -> bool:
        return self.voice_out or self.input_mode == "voice"

    async def state(self, state: str, detail: str | None = None) -> None:
        frame: dict[str, Any] = {"type": "state", "turn_id": self.turn_id, "state": state}
        if detail:
            frame["detail"] = detail
        await self.emit(frame)

    def final_text(self) -> str:
        """Interrupted voice turn with a playback report -> only what was played; otherwise what was streamed."""
        if self.interrupted and self.chunks and self.played_seq is not None:
            return " ".join(t for s, t in sorted(self.chunks.items()) if s <= self.played_seq).strip()
        raw = self.assistant_text.strip()
        return (
            policy.clean_answer(raw, self.source_texts, self.user_text, self.voice, self.intent, self.ticket_offer)
            if raw
            else raw
        )


class _Speech:
    """Sentence queue -> TTS worker. Runs concurrently with the LLM stream; chunks leave strictly in order."""

    def __init__(self, svc: "ConversationService", ts: TurnState) -> None:
        self._svc, self._ts = svc, ts
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._pending: list[str] = []  # deferred while the LLM holds the RAM (strict single model)
        self._seq = 0
        self._failed = False
        self._task = asyncio.create_task(self._run())

    def push(self, sentence: str) -> None:
        self._queue.put_nowait(sentence)

    async def finish(self) -> None:
        self._queue.put_nowait(None)
        await self._task

    async def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while (s := await self._queue.get()) is not None:
            if self._failed:
                continue
            if self._pending:
                self._pending.append(s)
                continue
            try:
                await self._speak(s)
            except ConflictError:  # TTS cannot be loaded while the LLM is busy: retry after the LLM is done
                self._pending.append(s)
            except _EXPECTED_ERRORS as e:
                await self._fail(e)
        for s in self._pending:
            if self._failed:
                break
            try:
                await self._speak(s)
            except _EXPECTED_ERRORS as e:
                await self._fail(e)

    async def _fail(self, e: Exception) -> None:
        self._failed = True
        await self._ts.emit({"type": "error", "turn_id": self._ts.turn_id, "detail": f"TTS: {e}"})

    async def _speak(self, sentence: str) -> None:
        ts = self._ts
        text = clean_for_speech(sentence)
        if not text:
            return
        async with self._svc.lease("tts", ts) as lease:
            p = lease.profile.params
            audio = await lease.adapter.synthesize(lease.profile.model_ref, text, p.get("voice"), "wav", p)
        if ts.first_audio_ms is None:
            ts.first_audio_ms = _ms(ts.t_llm or ts.t_start)
            await ts.state("speaking")
        seq, self._seq = self._seq, self._seq + 1
        ts.chunks[seq] = sentence
        await ts.emit(
            {
                "type": "audio_chunk",
                "turn_id": ts.turn_id,
                "seq": seq,
                "mime": "audio/wav",
                "data": base64.b64encode(audio).decode(),
                "text": sentence,
            }
        )


class ConversationService:
    def __init__(
        self,
        repo: ConversationRepo,
        manager: Any,
        tools: ToolRegistry,
        settings: SettingsReader,
        knowledge: KnowledgeSearchPort | None = None,
        *,
        history_limit: int = 12,
        agent: Any = None,
        tickets: CaseTicketPort | None = None,
        proactive: ProactivePort | None = None,
    ) -> None:
        self.repo, self.manager, self.tools, self.settings = repo, manager, tools, settings
        self.knowledge = knowledge
        self._history_limit = history_limit
        self._title_tasks: set[asyncio.Task] = set()  # background LLM chat titles (kept referenced until done)
        # Agent core (PART 2). None -> legacy deterministic pipeline only.
        self.case_agent = CaseAgent(agent, tickets, proactive) if agent is not None else None

    @property
    def proactive(self) -> ProactivePort:
        return self.case_agent.proactive if self.case_agent else NoopProactive()

    def attach_proactive(self, proactive: ProactivePort | None) -> None:
        """Called by the WS router with app.state.proactive (set by main.py's lifespan when available)."""
        if proactive is not None and self.case_agent is not None:
            self.case_agent.proactive = proactive

    # ------------------------------------------------------------------ sessions
    async def open_session(self, session_id: str | None, voice: bool) -> str:
        if session_id:
            try:
                sid = str(UUID(session_id))
            except ValueError:
                sid = None
            if sid and await self.repo.session_exists(sid):
                return sid
        return await self.repo.create_session("voice" if voice else "web")

    async def list_sessions(self, limit: int = 50, include_archived: bool = False) -> list[SessionOut]:
        return [SessionOut(**{**r, "id": str(r["id"])}) for r in await self.repo.list_sessions(limit, include_archived)]

    async def create_session(self) -> SessionOut:
        return await self._session_out(await self.repo.create_session("web"))

    async def _session_out(self, session_id: str) -> SessionOut:
        row = await self.repo.get_session(session_id) if _uuid(session_id) else None
        if row is None:
            raise NotFoundError("session not found")
        return SessionOut(**{**row, "id": str(row["id"])})

    async def update_session(self, session_id: str, changes: dict[str, Any]) -> SessionOut:
        if not _uuid(session_id) or not await self.repo.update_session(session_id, changes):
            raise NotFoundError("session not found")
        return await self._session_out(session_id)

    async def delete_session(self, session_id: str) -> None:
        if not _uuid(session_id) or not await self.repo.delete_session(session_id):
            raise NotFoundError("session not found")

    async def get_messages(self, session_id: str) -> list[MessageOut]:
        rows = await self.repo.list_messages(session_id)
        if rows is None:
            raise NotFoundError("session not found")
        out: list[MessageOut] = []
        pending: dict[tuple, Any] = {}
        ui_pos: dict[str, int] = {}
        for r in rows:
            if r["role"] == "tool":
                try:
                    for s in json.loads(r["content"]).get("sources") or []:
                        src = as_source(s)
                        pending[(src.title, src.source_uri, src.section)] = src
                except (ValueError, AttributeError):
                    pass
            elif r["role"] == "ui":
                try:
                    c = json.loads(r["content"])
                except (ValueError, TypeError):
                    continue
                entry = MessageOut(id=c.get("id") or str(r["id"]), role="ui", content=c.get("fallback_text") or "",
                                   input_mode=r["input_mode"], created_at=r["created_at"], component=c.get("component"),
                                   props=c.get("props"), fallback_text=c.get("fallback_text"))  # fmt: skip
                prev = ui_pos.get(entry.id)
                if prev is None:  # same id = live update: keep the first position, the latest props
                    ui_pos[entry.id] = len(out)
                    out.append(entry)
                else:
                    out[prev] = entry.model_copy(update={"created_at": out[prev].created_at})
            elif r["role"] in ("user", "assistant"):
                srcs = list(pending.values()) if r["role"] == "assistant" and pending else None
                if r["role"] == "assistant":
                    pending = {}
                out.append(MessageOut(**{**r, "id": str(r["id"]), "sources": srcs}))
        return out

    async def ui_props(self, session_id: str, component_id: str | None) -> dict[str, Any] | None:
        """Latest props emitted for a component id in this session (from the persisted 'ui' rows)."""
        if not component_id:
            return None
        try:
            rows = await self.repo.list_messages(session_id) or []
        except Exception:
            return None
        for r in reversed(rows):
            if r.get("role") != "ui":
                continue
            try:
                c = json.loads(r["content"])
            except (ValueError, TypeError):
                continue
            if c.get("id") == component_id:
                return c.get("props")
        return None

    async def emit_ui(self, ts: TurnState, components: list[dict[str, Any]]) -> None:
        """S->C `ui_component` frames (persisted in finish()); in voice mode `spoken` is said, never the props."""
        for c in components:
            await ts.emit({**c, "turn_id": ts.turn_id})
            ts.ui_emitted.append(c)
            if ts.voice_out and c.get("spoken"):
                await self._speak_only(ts, c["spoken"])

    async def _speak_only(self, ts: TurnState, text: str) -> None:
        speech = _Speech(self, ts)
        try:
            splitter = SentenceSplitter()
            for s in [*splitter.feed(text), splitter.flush() or ""]:
                if f := policy.filter_sentence(s):
                    speech.push(f)
            await speech.finish()
        finally:
            await speech.cancel()

    def new_turn(self, session_id: str, turn_id: str, voice_out: bool, input_mode: str, emit: Emit) -> TurnState:
        return TurnState(turn_id, session_id, voice_out, emit, input_mode)

    # ------------------------------------------------------------------ model leases
    @staticmethod
    def _load_notice(ts: TurnState, role: str) -> Callable[[], Awaitable[None]]:
        async def notice() -> None:
            await ts.state("loading_model", role)

        return notice

    @asynccontextmanager
    async def lease(self, role: str, ts: TurnState) -> AsyncIterator[Lease]:
        """manager.use(role); `loading_model` is emitted only when the manager really loads a model."""
        async with self.manager.use(role, on_load=self._load_notice(ts, role)) as lease:
            yield lease

    # ------------------------------------------------------------------ turn
    async def run_turn(self, ts: TurnState, req: TurnRequest) -> None:
        try:
            await self._body(ts, req)
        except asyncio.CancelledError:
            ts.interrupted = True
        except _EXPECTED_ERRORS as e:
            ts.errored = True
            await ts.emit({"type": "error", "turn_id": ts.turn_id, "detail": str(e)})
        except Exception:
            log.exception("turn %s failed", ts.turn_id)
            ts.errored = True
            await ts.emit({"type": "error", "turn_id": ts.turn_id, "detail": "internal error"})
        await asyncio.shield(self.finish(ts))
        if ts.title_seed and not (ts.interrupted or ts.errored) and await self._title_enabled():
            task = asyncio.create_task(self._llm_title(ts, *ts.title_seed))
            self._title_tasks.add(task)
            task.add_done_callback(self._title_tasks.discard)

    async def _title_enabled(self) -> bool:
        try:
            return await self.settings.get("chat_title_llm", True) is not False
        except Exception:
            return True

    async def _llm_title(self, ts: TurnState, first_text: str, fallback: str) -> None:
        """Short chat title from the first message, written only while the fallback title is still in place."""
        prompt = [
            {"role": "system", "content": (
                "Escribe un título breve (máximo 6 palabras) en español para una conversación de soporte de red que "
                "empieza con el mensaje del usuario. Solo el título: sin comillas, sin punto final, sin datos personales.")},
            {"role": "user", "content": first_text[:600]},
        ]  # fmt: skip
        raw = ""
        try:
            async with self.lease("llm", ts) as lease:
                params = {**lease.profile.params, "max_tokens": 40, "temperature": 0.2}
                async for d in lease.adapter.chat(lease.profile.model_ref, prompt, tools=None, stream=True, params=params):
                    raw += d.content or ""
            title = re.sub(r"\s+", " ", raw).strip().strip("\"'«»*#.").strip()[:TITLE_CHARS]
            if not title:
                return
            current = await self.repo.get_session(ts.session_id)
            if current and current.get("title") == fallback:  # user may have renamed it meanwhile
                await self.repo.update_session(ts.session_id, {"title": title})
        except Exception as e:  # cosmetic: never surfaces
            log.info("llm title skipped: %s", type(e).__name__)

    async def finish(self, ts: TurnState) -> None:
        """Persist what is due, emit turn_end + idle. Idempotent (also used when a turn is cancelled before starting)."""
        if ts.ended:
            return
        ts.ended = True
        total = _ms(ts.t_start)
        text = ts.final_text()
        try:
            if text:
                ts.assistant_message_id = await self.repo.add_message(
                    ts.session_id, "assistant", text, ts.input_mode, ts.llm_profile_id
                )
            for c in ts.ui_emitted:
                await self.repo.add_message(ts.session_id, "ui", json.dumps(
                    {k: c.get(k) for k in ("id", "component", "props", "spoken", "fallback_text")},
                    ensure_ascii=False, default=str), ts.input_mode, None)  # fmt: skip
            if ts.input_mode == "voice" or ts.voice_out:
                ts.voice_turn_id = await self.repo.add_voice_turn(
                    ts.session_id,
                    ts.user_text or None,
                    ts.interrupted,
                    ts.stt_ms,
                    ts.rag_ms,
                    ts.first_token_ms,
                    ts.first_audio_ms,
                )
        except Exception as e:
            log.warning("could not persist turn %s: %s", ts.turn_id, type(e).__name__)
        voseo = policy.voseo_hits(text)
        if voseo:
            log.warning("turn %s: voseo in answer: %s", ts.turn_id, voseo)
        ts.rewritten = bool(text) and text != ts.streamed.strip()
        ts.citations_removed = max(0, len(_CITE_RE.findall(ts.streamed)) - len(_CITE_RE.findall(text)))
        await self._record_metrics(ts, total, len(voseo))
        frame: dict[str, Any] = {
            "type": "turn_end",
            "turn_id": ts.turn_id,
            "interrupted": ts.interrupted,
            "metrics": {
                "stt_ms": ts.stt_ms,
                "rag_ms": ts.rag_ms,
                "llm_first_token_ms": ts.first_token_ms,
                "tts_first_audio_ms": ts.first_audio_ms,
                "total_ms": total,
            },
        }
        if text and text != ts.streamed.strip():  # cleaned/validated answer differs from what was streamed
            frame["final_text"] = text
        await ts.emit(frame)
        await ts.state("idle")

    async def _record_metrics(self, ts: TurnState, total: int, voseo: int) -> None:
        """One turn_metrics row per turn; best effort (never breaks the turn)."""
        try:
            await self.repo.add_turn_metrics(
                {
                    "session_id": ts.session_id,
                    "turn_id": ts.turn_id,
                    "input_mode": ts.input_mode,
                    "intent": ts.intent,
                    "audience": ts.audience,
                    "model_profile_id": ts.llm_profile_id,
                    "stt_ms": ts.stt_ms,
                    "rag_ms": ts.rag_ms,
                    "llm_first_token_ms": ts.first_token_ms,
                    "tts_first_audio_ms": ts.first_audio_ms,
                    "total_ms": total,
                    "prompt_tokens": ts.prompt_tokens,
                    "cached_tokens": ts.cached_tokens,
                    "completion_tokens": ts.completion_tokens,
                    "finish_reason": ts.finish_reason,
                    "sources_count": ts.sources_count,
                    "top_source_score": ts.top_source_score,
                    "tool_calls": ts.tool_calls,
                    "tool_errors": ts.tool_errors,
                    "interrupted": ts.interrupted,
                    "voseo_hits": voseo,
                    "citations_removed": ts.citations_removed,
                    "final_text_rewritten": ts.rewritten,
                    "case_state": ts.case_snapshot,
                }
            )
        except Exception as e:
            log.warning("could not record turn metrics %s: %s", ts.turn_id, type(e).__name__)

    async def cancel_finished(self, ts: TurnState) -> None:
        """Barge-in after the server already sent everything: trim the stored answer to what the client played."""
        if not ts.ended or ts.interrupted or not ts.chunks or ts.played_seq is None:
            return
        if ts.played_seq >= max(ts.chunks):
            return
        ts.interrupted = True
        played = ts.final_text()
        try:
            if ts.assistant_message_id and played:
                await self.repo.update_message_content(ts.assistant_message_id, played)
            if ts.voice_turn_id:
                await self.repo.mark_voice_turn_interrupted(ts.voice_turn_id)
        except Exception as e:
            log.warning("late cancel persist failed: %s", type(e).__name__)

    async def _settings(self) -> tuple[str, int, str, float, str, str]:
        sys_prompt = await self.settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        top_k = await self.settings.get("rag_top_k", 5)
        top_k = top_k if isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0 else 5
        mode = await self.settings.get("rag_mode", "auto")
        mode = mode if mode in RAG_MODES else "auto"
        min_score = await self.settings.get("rag_min_score", DEFAULT_MIN_SCORE)
        if isinstance(min_score, bool) or not isinstance(min_score, int | float) or not 0 <= min_score <= 1:
            min_score = DEFAULT_MIN_SCORE
        level = await self.settings.get("audience_level", "auto")
        trato = await self.settings.get("trato", "usted")
        return (
            sys_prompt,
            top_k,
            mode,
            float(min_score),
            level if level in ("auto", "basico", "tecnico") else "auto",
            trato if trato in ("usted", "tu") else "usted",
        )

    async def _body(self, ts: TurnState, req: TurnRequest) -> None:
        sys_prompt, top_k, rag_mode, min_score, level_setting, trato = await self._settings()
        try:  # answer length limits per turn type (UI-editable); None -> code defaults
            limits = await self.settings.get("max_tokens_by_intent", None)
        except Exception:
            limits = None
        ts.token_limits = limits if isinstance(limits, dict) else None

        agent_on = self.case_agent is not None
        ts.progress_on, ts.confirm = agent_on, req.confirm
        contact: str | None = None
        if req.audio is not None:
            ts.input_mode = "voice"
            text = await self._transcribe(ts, req)
            if agent_on:  # contacts never reach messages, transcripts, traces or metrics
                text, contact = agent_policy.redact_contact(text)
            await ts.emit({"type": "transcript", "turn_id": ts.turn_id, "text": text})
            if not text:
                await ts.emit({"type": "error", "turn_id": ts.turn_id, "detail": "no se detectó voz en el audio"})
                return
        elif req.confirm is not None:
            text = "Sí" if req.confirm else "No"
        elif req.ui_event is not None:
            if self.case_agent is None:
                await ts.emit({"type": "error", "turn_id": ts.turn_id, "detail": "los componentes requieren el agente"})
                return
            ts.ui_event = req.ui_event
            got = await self.case_agent.perceive_ui(self, ts, req.ui_event)
            if not got:  # invalid event (error already emitted) or metric-only event: no reply
                return
            text = got
        elif req.media_id is not None:
            got = await self._media_text(ts, req)
            if got is None:
                return
            text, contact = got
        else:
            text = req.text or ""
            if agent_on:
                text, contact = agent_policy.redact_contact(text)
        ts.user_text = text

        rows = await self.repo.recent_messages(ts.session_id, self._history_limit)
        user_rows = [m["content"] for m in rows if m["role"] == "user"]
        prev_assistant = next((m["content"] for m in reversed(rows) if m["role"] == "assistant"), "")
        prev_user = next(
            (t for t in reversed(user_rows) if policy.classify_intent(t) not in (policy.SMALLTALK, policy.CLOSING)),
            None,
        )
        await self.repo.add_message(ts.session_id, "user", text, ts.input_mode, ts.stt_profile_id)
        try:
            fallback_title = make_title(text)
            await self.repo.touch_session(ts.session_id, fallback_title)
            if not user_rows and policy.classify_intent(text) not in (policy.SMALLTALK, policy.CLOSING):
                ts.title_seed = (text, fallback_title)
        except Exception as e:  # chat-list bookkeeping must never break a turn
            log.warning("could not touch session %s: %s", ts.session_id, type(e).__name__)

        intent = policy.classify_intent(text, prev_assistant)
        level = policy.audience(level_setting, [*user_rows, text])
        snap = policy.case_snapshot([*user_rows, text], level, intent)
        ts.intent, ts.audience, ts.case_snapshot = intent, level, snap  # known even if the turn is cancelled later
        plan: AgentPlan | None = None
        if self.case_agent is not None:
            try:
                plan = await self.case_agent.before(
                    self, ts, text, contact, intent, level, snap, [*user_rows, text], trato
                )
            except Exception as e:  # the agent core degrades to the legacy pipeline, never kills the turn
                log.warning("agent cycle failed in turn %s: %s: %s", ts.turn_id, type(e).__name__, e)
                plan = None
        if plan is not None:
            intent = plan.intent or intent
            snap["case"] = plan.snapshot or None
            ts.intent = intent
        legacy = plan is None or plan.passthrough
        if plan is not None and plan.fixed_text is not None:
            if plan.confirm:
                await ts.emit(plan.confirm)
            await self._say_fixed(ts, plan.fixed_text)
            await self.emit_ui(ts, plan.components)
            if plan.case_update:
                await ts.emit(plan.case_update)
            return
        existing: tuple[str, str] | None = None
        ticket_tail = ""
        if legacy and intent in (policy.DIAGNOSTIC, policy.GUIDE, policy.OUTPUT, policy.CLARIFY, policy.TICKET_CREATE):
            existing = await self._session_ticket(ts.session_id)
        if not legacy:
            assert plan is not None
            ts.ticket_offer = plan.ticket_offer
            snap["ticket_offer"] = ts.ticket_offer
        elif intent == policy.TICKET_CREATE:
            created = existing is None
            if existing is None:
                existing = await self._auto_create_ticket(ts, [*user_rows, text], snap)
            if existing is not None:
                ticket_tail = policy.ticket_created_instruction(existing[0], existing[1], created, trato)
            else:  # creation failed: tell the truth, never claim a ticket
                ticket_tail = (
                    "INSTRUCCIÓN (no la menciones): No se pudo registrar el ticket por un error del sistema. Dígaselo "
                    "en 1–2 frases, pídale disculpas y sugiera contactar a su proveedor por ahora. "
                    + policy._TRATO.get(trato, "")
                )
            ts.ticket_offer = False
        elif intent in (policy.DIAGNOSTIC, policy.GUIDE, policy.OUTPUT, policy.CLARIFY):
            offered = [policy.offered_ticket(m["content"]) for m in rows if m["role"] == "assistant"]
            ts.ticket_offer = False if existing else policy.ticket_decision(text, snap, offered, bool(offered))
            snap["ticket_offer"], snap["ticket_offers_before"] = ts.ticket_offer, sum(offered)
        hits: list[KnowledgeHit] = []
        want_rag = intent not in policy.NO_RAG_INTENTS and (legacy or (plan is not None and plan.rag))
        if rag_mode in ("auto", "both") and self.knowledge is not None and want_rag:
            hits = await self._retrieve(ts, text, prev_user, top_k, min_score, intent == policy.CLARIFY)
        ts.source_texts = [h.content for h in hits]
        ts.sources_count = len(hits)
        ts.top_source_score = round(max((h.score for h in hits), default=0.0), 4) if hits else None
        step = policy.last_step(prev_assistant) if intent == policy.GUIDE else None
        tail = (
            (plan.tail if plan is not None and plan.tail else None)
            or ticket_tail
            or policy.instruction(
                intent,
                level,
                ts.voice,
                bool(hits),
                trato,
                step,
                ts.ticket_offer,
                policy.is_apipa("\n".join([*user_rows, text])),
                snap,
            )
        )
        if intent in (policy.DIAGNOSTIC, policy.GUIDE, policy.OUTPUT, policy.CLARIFY) and (
            state := policy.render_case_state(snap)
        ):
            tail += f"\n\n{state}"
        if plan is not None and plan.extra_tail:
            tail += f"\n\n{plan.extra_tail}"
        if hits and (legacy or (plan is not None and plan.context)):
            tail += f"\n\n{context_block(hits)}"
        if hint := page_hint(req.context):
            tail = f"{hint}\n\n{tail}"
        # Byte-stable prefix (system + tools + history exactly as the user wrote it/the model answered) first; the
        # per-turn instruction + retrieved sources go in a SEPARATE trailing message, so next turn only that tail differs.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_prompt + RULES_ADDENDUM + (TOOL_RAG_RULE if rag_mode != "auto" else "")},
            *trim_history(rows),
            {"role": "user", "content": text},
            {"role": "user", "content": tail},
        ]
        exclude = {"search_knowledge"} if rag_mode == "auto" else set()
        if not legacy:  # agent mode: tickets are created by the agent core (K3 + confirmation), never by the LLM
            exclude.add("create_ticket")
        if CONDITIONAL_TOOLS:
            exclude |= policy.tool_exclusions(intent, self.tools.names(), text)
        ts.max_tokens = (plan.max_tokens if plan else None) or policy.max_tokens_for(
            intent, ts.voice, DEFAULT_MAX_TOKENS, ts.token_limits
        )
        log.info("turn %s: intent=%s level=%s tools_excluded=%d", ts.turn_id, intent, level, len(exclude))
        await self._llm_loop(ts, messages, ToolContext(ts.session_id, ts.turn_id, top_k), exclude)
        if plan is not None and plan.fallback_text and not ts.assistant_text.strip():
            await self._say_fixed(ts, plan.fallback_text)
        if not ts.assistant_text.strip():  # e.g. a reasoning model spent the whole budget thinking
            log.warning("turn %s: empty answer (finish=%s); sending generic fallback", ts.turn_id, ts.finish_reason)
            await self._say_fixed(ts, EMPTY_ANSWER_FALLBACK)
        if plan is not None:
            await self.emit_ui(ts, plan.components)
            if plan.case_update:
                await ts.emit(plan.case_update)

    async def _media_text(self, ts: TurnState, req: TurnRequest) -> tuple[str, str | None] | None:
        """media_message: audio -> its transcript is the user text; image -> photo facts + image_annotated (agent).
        Missing media / analysis degrade to the text the person wrote (or a neutral sentence)."""
        typed = (req.text or "").strip()
        media = None
        try:
            getter = getattr(self.repo, "get_media", None)
            media = await getter(req.media_id, ts.session_id) if getter else None
        except Exception as e:
            log.warning("media lookup failed: %s", type(e).__name__)
        contact: str | None = None
        if media is not None and media.get("kind") == "audio":
            ts.input_mode = "voice"
            text = (media.get("transcript") or "").strip() or typed
            if self.case_agent is not None:
                text, contact = agent_policy.redact_contact(text)
            await ts.emit({"type": "transcript", "turn_id": ts.turn_id, "text": text})
            if not text:
                await ts.emit({"type": "error", "turn_id": ts.turn_id, "detail": "no se pudo transcribir el audio"})
                return None
            return text, contact
        if self.case_agent is not None:
            typed, contact = agent_policy.redact_contact(typed)
            return await self.case_agent.perceive_media(self, ts, media, typed), contact
        if not typed:
            await ts.emit({"type": "error", "turn_id": ts.turn_id, "detail": "adjunto sin texto"})
            return None
        return typed, None

    async def _progress(self, ts: TurnState, stage: str) -> None:
        """I1: one `progress` frame per stage, agent mode only."""
        if not ts.progress_on or stage in ts.progress_sent or stage not in PROGRESS:
            return
        ts.progress_sent.add(stage)
        await ts.emit({"type": "progress", "turn_id": ts.turn_id, "message": PROGRESS[stage]})

    async def _say_fixed(self, ts: TurnState, text: str) -> None:
        """Deterministic answer (K3 question, confirmation, fallback): streamed and spoken like an LLM answer."""
        if ts.first_token_ms is None:
            ts.first_token_ms = _ms(ts.t_llm or ts.t_start)
        sep = " " if ts.assistant_text and not ts.assistant_text.endswith((" ", "\n")) else ""
        ts.assistant_text += sep + text
        ts.streamed += sep + text
        await ts.emit({"type": "token", "turn_id": ts.turn_id, "text": sep + text})
        if ts.voice_out:
            speech = _Speech(self, ts)
            try:
                splitter = SentenceSplitter()
                for s in [*splitter.feed(text), splitter.flush() or ""]:
                    if f := policy.filter_sentence(s):
                        speech.push(f)
                await speech.finish()
            finally:
                await speech.cancel()

    async def _session_ticket(self, session_id: str) -> tuple[str, str] | None:
        """(ticket_number, priority) of the ticket already created in this session (tool rows), else None."""
        try:
            rows = await self.repo.list_messages(session_id) or []
        except Exception as e:
            log.warning("could not look up session ticket: %s", type(e).__name__)
            return None
        for m in reversed(rows):
            if m.get("role") != "tool":
                continue
            try:
                d = json.loads(m["content"])
            except (ValueError, TypeError):
                continue
            if d.get("name") == "create_ticket" and d.get("ok"):
                mt = re.search(r"TRM-\d+", d.get("summary") or "")
                if mt:
                    return mt.group(0), str((d.get("arguments") or {}).get("priority", "medium"))
        return None

    async def _auto_create_ticket(self, ts: TurnState, user_texts: list[str], snap: dict) -> tuple[str, str] | None:
        """Backend-driven create_ticket (same registry path: audited, tool_call/tool_result frames, persisted tool row)."""
        args = policy.build_ticket(user_texts, snap)
        scratch: list[dict[str, Any]] = []
        ctx = ToolContext(ts.session_id, ts.turn_id, 3)
        await self._run_tools(ts, scratch, [ToolCall("create_ticket", args, "auto_ticket")], "", ctx, False)
        try:
            d = json.loads(scratch[-1]["content"])
        except (ValueError, KeyError, IndexError):
            return None
        return (d["ticket_number"], str(d.get("priority", args["priority"]))) if d.get("created") else None

    async def _retrieve(
        self, ts: TurnState, text: str, prev_user: str | None, top_k: int, min_score: float, clarify: bool = False
    ) -> list[KnowledgeHit]:
        """Deterministic RAG: always search; an elliptical follow-up ("ya revisé, otros sí navegan") is retried
        together with the previous user message. Failures degrade to "no context", never kill the turn."""
        assert self.knowledge is not None
        await ts.state("retrieving")
        await self._progress(ts, "retrieving")
        t0 = time.perf_counter()
        on_load = self._load_notice(ts, "embedding")
        query = f"{prev_user}\n{text}" if clarify and prev_user else text
        hits: list[KnowledgeHit] = []
        try:
            hits = await self.knowledge.search(policy.expand_query(query), top_k, on_load=on_load)
            best = max((h.score for h in hits), default=0.0)
            short = len(text.split()) < SHORT_QUERY_WORDS and not _TICKET_RE.search(text)
            if best < min_score and short and prev_user:
                query = f"{prev_user}\n{text}"
                more = await self.knowledge.search(policy.expand_query(query), top_k, on_load=on_load)
                if max((h.score for h in more), default=0.0) > best:
                    hits = more
        except Exception as e:
            log.warning("knowledge search failed: %s", e)
            hits = []
        best = max((h.score for h in hits), default=0.0)
        hits = [h for h in hits if h.score >= max(min_score, best - RAG_REL_MARGIN)]
        hits = [
            hits[i]
            for i in policy.outage_boost(
                query, [f"{h.title} {h.section or ''}" for h in hits], [h.content for h in hits]
            )
        ][:RAG_MAX_HITS]
        ts.rag_ms = _ms(t0)
        sources = [_source(h) for h in hits]
        await ts.emit({"type": "sources", "turn_id": ts.turn_id, "items": sources})
        if sources:  # same row shape as a search_knowledge tool call, so history shows the sources
            await self.repo.add_message(
                ts.session_id,
                "tool",
                json.dumps(
                    {
                        "name": "search_knowledge",
                        "arguments": {"query": query},
                        "ok": True,
                        "auto": True,
                        "summary": f"{len(hits)} fragmento(s) encontrados",
                        "sources": sources,
                    },
                    ensure_ascii=False,
                    default=str,
                ),  # fmt: skip
                ts.input_mode,
                None,
            )
        await ts.state("thinking")
        return hits

    async def _transcribe(self, ts: TurnState, req: TurnRequest) -> str:
        await ts.state("transcribing")
        t0 = time.perf_counter()
        async with self.lease("stt", ts) as lease:
            p = lease.profile.params
            ts.stt_profile_id = lease.profile.id
            text = await lease.adapter.transcribe(
                lease.profile.model_ref, req.audio or b"", req.mime, p.get("language"), p
            )
        ts.stt_ms = _ms(t0)
        return text.strip()

    async def _llm_loop(
        self, ts: TurnState, messages: list[dict[str, Any]], ctx: ToolContext, exclude: set[str] | None = None
    ) -> None:
        speech = _Speech(self, ts) if ts.voice_out else None
        splitter = SentenceSplitter()
        defs = self.tools.definitions(exclude or set()) or None
        ts.t_llm = time.perf_counter()

        async def say(text: str) -> None:
            if ts.first_token_ms is None:
                ts.first_token_ms = _ms(ts.t_llm or ts.t_start)
            ts.assistant_text += text
            ts.streamed += text
            await ts.emit({"type": "token", "turn_id": ts.turn_id, "text": text})
            if speech:
                for s in splitter.feed(text):
                    if f := policy.filter_sentence(s):
                        speech.push(f)

        try:
            for it in range(MAX_TOOL_ITERATIONS):
                await ts.state("thinking")
                await self._progress(ts, "thinking")
                calls = []
                text_this = ""
                finish: str | None = None
                async with self.lease("llm", ts) as lease:  # released before tools run (RAM may be needed elsewhere)
                    ts.llm_profile_id = lease.profile.id
                    ollama = lease.adapter.kind == "ollama"
                    last = it == MAX_TOOL_ITERATIONS - 1
                    params = dict(lease.profile.params)
                    params["max_tokens"] = ts.max_tokens or params.get("max_tokens") or DEFAULT_MAX_TOKENS
                    stream = lease.adapter.chat(
                        lease.profile.model_ref,
                        messages,
                        tools=None if last else defs,
                        stream=True,
                        params=params,
                    )
                    async for d in stream:
                        if d.content:
                            text_this += d.content
                            await say(d.content)
                        calls.extend(d.tool_calls)
                        finish = d.finish_reason or finish
                        ts.add_usage(d.usage)
                ts.finish_reason = finish
                if finish == "length" and not calls:  # hit max_tokens: never leave a dangling half sentence
                    log.info("turn %s: answer cut by max_tokens", ts.turn_id)
                    if ts.assistant_text.strip() and ts.assistant_text.rstrip()[-1] not in ".!?…":
                        await say(TRUNCATED_SUFFIX)
                if not calls or last:
                    break
                ts.assistant_text = ""  # only the final iteration is the answer (pre-tool text would glue two answers)
                await self._run_tools(ts, messages, calls, text_this, ctx, ollama)
            if speech:
                if tail := policy.filter_sentence(splitter.flush() or ""):
                    speech.push(tail)
                await speech.finish()
        finally:
            if speech:
                await speech.cancel()

    async def _run_tools(self, ts, messages, calls, text_this, ctx, ollama) -> None:  # type: ignore[no-untyped-def]
        ids = [c.id or f"call_{i}" for i, c in enumerate(calls)]
        messages.append(
            {
                "role": "assistant",
                "content": text_this,
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments if ollama else json.dumps(c.arguments)},
                    }
                    for cid, c in zip(ids, calls, strict=True)
                ],
            }
        )
        for cid, c in zip(ids, calls, strict=True):
            await ts.emit({"type": "tool_call", "turn_id": ts.turn_id, "name": c.name, "arguments": c.arguments})
            await ts.state("retrieving" if c.name == "search_knowledge" else "thinking", c.name)
            res = await self.tools.execute(c.name, c.arguments, ctx)
            ts.tool_calls += 1
            ts.tool_errors += 0 if res.ok else 1
            if res.phase == "retrieving":
                ts.rag_ms = (ts.rag_ms or 0) + res.duration_ms
            await ts.emit(
                {"type": "tool_result", "turn_id": ts.turn_id, "name": c.name, "ok": res.ok, "summary": res.summary}
            )
            if res.sources:
                await ts.emit({"type": "sources", "turn_id": ts.turn_id, "items": res.sources})
            messages.append(
                {"role": "tool", "tool_name": c.name, "content": res.content}
                if ollama
                else {"role": "tool", "tool_call_id": cid, "content": res.content}
            )
            await self.repo.add_message(
                ts.session_id,
                "tool",
                json.dumps(
                    {
                        "name": c.name,
                        "arguments": c.arguments,
                        "ok": res.ok,
                        "summary": res.summary,
                        "sources": res.sources,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                ts.input_mode,
                None,
            )
            await ts.state("thinking")
