"""Per-connection controller: parses client frames, owns the running turn Task, implements barge-in/cancel."""

import asyncio
import base64
import binascii
import logging
from typing import Any

from pydantic import ValidationError

from .protocol import (
    AudioSegment,
    Cancel,
    ConfirmResponse,
    MediaMessage,
    NotificationAck,
    PlaybackProgress,
    SetVoice,
    StartSession,
    TextMessage,
    UiEvent,
    client_frame,
)
from .service import ConversationService, Emit, TurnRequest, TurnState

log = logging.getLogger("trama.conversation")


class SessionController:
    def __init__(self, svc: ConversationService, send: Emit) -> None:
        self._svc = svc
        self._send = send
        self.session_id: str | None = None
        self.voice = False
        self._turns: dict[str, tuple[TurnState, asyncio.Task]] = {}
        self._last: TurnState | None = None
        self._registered: str | None = None  # session registered with the NotificationHub

    def _register(self) -> None:
        """Proactive messages (followups, technician notes) are pushed to the open socket of this session."""
        if self.session_id == self._registered:
            return
        self._unregister()
        try:
            self._svc.proactive.hub.register(self.session_id or "", self._send)
            self._registered = self.session_id
        except Exception as e:
            log.warning("notification hub register failed: %s", type(e).__name__)

    def _unregister(self) -> None:
        if self._registered is None:
            return
        try:
            self._svc.proactive.hub.unregister(self._registered)
        except Exception as e:
            log.warning("notification hub unregister failed: %s", type(e).__name__)
        self._registered = None

    async def _error(self, detail: str, turn_id: str | None = None) -> None:
        frame: dict[str, Any] = {"type": "error", "detail": detail}
        if turn_id:
            frame["turn_id"] = turn_id
        await self._send(frame)

    async def handle(self, raw: Any) -> None:
        try:
            msg = client_frame.validate_python(raw)
        except ValidationError as e:
            first = e.errors()[0]
            await self._error(f"frame inválido: {'.'.join(str(p) for p in first['loc'])}: {first['msg']}")
            return
        match msg:
            case StartSession():
                await self._cancel_all()
                self.voice = msg.voice
                self.session_id = await self._svc.open_session(msg.session_id, msg.voice)
                await self._send({"type": "session", "session_id": self.session_id})
                self._register()
            case ConfirmResponse():
                await self._start_turn(f"{msg.turn_id}-confirm", TurnRequest(confirm=msg.accepted), "text")
            case NotificationAck():
                try:
                    await self._svc.proactive.ack(msg.id)
                except Exception as e:
                    log.warning("notification ack failed: %s", type(e).__name__)
            case SetVoice():
                self.voice = msg.voice
            case PlaybackProgress():
                ts = self._find(msg.turn_id)
                if ts:
                    ts.played_seq = max(msg.played_seq, ts.played_seq if ts.played_seq is not None else -1)
            case Cancel():
                await self._cancel(msg.turn_id)
            case TextMessage():
                await self._start_turn(msg.turn_id, TurnRequest(text=msg.text.strip(), context=msg.context.model_dump() if msg.context else None), "text")
            case UiEvent():
                event = msg.model_dump(exclude={"type", "turn_id"})
                await self._start_turn(msg.turn_id, TurnRequest(ui_event=event), "text")
            case MediaMessage():
                text = (msg.text or "").strip() or None
                await self._start_turn(msg.turn_id, TurnRequest(text=text, media_id=msg.media_id), "text")
            case AudioSegment():
                try:
                    audio = base64.b64decode(msg.data, validate=True)
                except (binascii.Error, ValueError):
                    await self._error("audio base64 inválido", msg.turn_id)
                    return
                await self._start_turn(msg.turn_id, TurnRequest(audio=audio, mime=msg.mime), "voice")

    async def close(self) -> None:
        self._unregister()
        await self._cancel_all()

    # ------------------------------------------------------------------
    def _find(self, turn_id: str) -> TurnState | None:
        if turn_id in self._turns:
            return self._turns[turn_id][0]
        return self._last if self._last and self._last.turn_id == turn_id else None

    async def _start_turn(self, turn_id: str, req: TurnRequest, mode: str) -> None:
        await self._cancel_all()  # barge-in
        if self.session_id is None:
            self.session_id = await self._svc.open_session(None, self.voice)
            await self._send({"type": "session", "session_id": self.session_id})
            self._register()
        ts = self._svc.new_turn(self.session_id, turn_id, self.voice, mode, self._send)
        task = asyncio.create_task(self._svc.run_turn(ts, req))
        self._turns[turn_id] = (ts, task)
        self._last = ts
        task.add_done_callback(lambda _t, tid=turn_id: self._turns.pop(tid, None))

    async def _cancel_all(self) -> None:
        for tid in list(self._turns):
            await self._cancel(tid)

    async def _cancel(self, turn_id: str) -> None:
        running = self._turns.get(turn_id)
        if running:
            ts, task = running
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if not ts.ended:  # cancelled before the coroutine ever ran
                ts.interrupted = True
                await self._svc.finish(ts)
        elif self._last and self._last.turn_id == turn_id:
            await self._svc.cancel_finished(self._last)
