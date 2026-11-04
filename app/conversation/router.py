import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect

from .deps import get_conversation_service
from .models import MessageOut, SessionOut, SessionPatch
from .service import ConversationService
from .session import SessionController

log = logging.getLogger("trama.conversation")
router = APIRouter(tags=["conversation"])
Svc = Annotated[ConversationService, Depends(get_conversation_service)]


@router.get("/api/sessions", response_model=list[SessionOut])
async def list_sessions(svc: Svc, limit: int = 50, include_archived: bool = False) -> list[SessionOut]:
    return await svc.list_sessions(max(1, min(limit, 200)), include_archived)


@router.post("/api/sessions", response_model=SessionOut, status_code=201)
async def create_session(svc: Svc) -> SessionOut:
    return await svc.create_session()


@router.patch("/api/sessions/{session_id}", response_model=SessionOut)
async def update_session(session_id: str, body: SessionPatch, svc: Svc) -> SessionOut:
    changes = body.model_dump(include=body.model_fields_set)
    if "title" in changes:
        changes["title"] = (changes["title"] or "").strip() or None  # blank -> NULL (re-titled on next message)
    if changes.get("archived") is None:
        changes.pop("archived", None)
    return await svc.update_session(session_id, changes)


@router.delete("/api/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, svc: Svc) -> Response:
    await svc.delete_session(session_id)
    return Response(status_code=204)


@router.get("/api/sessions/{session_id}/messages", response_model=list[MessageOut])
async def session_messages(session_id: str, svc: Svc) -> list[MessageOut]:
    return await svc.get_messages(session_id)


@router.websocket("/ws/session")
async def ws_session(ws: WebSocket, svc: Svc) -> None:
    await ws.accept()
    lock = asyncio.Lock()
    closed = False

    async def send(frame: dict[str, Any]) -> None:
        nonlocal closed
        if closed:
            return
        async with lock:
            try:
                await ws.send_json(frame)
            except Exception:  # client went away mid-stream
                closed = True

    svc.attach_proactive(getattr(ws.app.state, "proactive", None))
    ctl = SessionController(svc, send)
    try:
        while True:
            try:
                raw = json.loads(await ws.receive_text())
            except (json.JSONDecodeError, KeyError, TypeError):
                await send({"type": "error", "detail": "frame no es JSON válido"})
                continue
            try:
                await ctl.handle(raw)
            except WebSocketDisconnect:
                raise
            except Exception as e:  # never crash the socket
                log.exception("frame handling failed")
                await send({"type": "error", "detail": f"error interno: {type(e).__name__}"})
    except WebSocketDisconnect:
        pass
    finally:
        closed = True
        await ctl.close()
