import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.runtimes.base import RuntimeCallError

from .deps import get_model_manager
from .manager import ModelManager
from .models import LoadResult, PullBody, RuntimeStatus, TestResult

Mgr = Annotated[ModelManager, Depends(get_model_manager)]

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/status", response_model=RuntimeStatus)
async def status(mgr: Mgr):
    return await mgr.status()


@router.post("/profiles/{id}/load", response_model=LoadResult)
async def load(id: UUID, mgr: Mgr):
    return await mgr.load_profile(id)


@router.post("/profiles/{id}/unload", response_model=RuntimeStatus)
async def unload(id: UUID, mgr: Mgr):
    return await mgr.unload_profile(id)


@router.post("/profiles/{id}/test", response_model=TestResult)
async def test(id: UUID, request: Request, mgr: Mgr):
    """Optional raw audio body (Content-Type: audio/*) is used as the sample for stt profiles."""
    audio = await request.body() or None
    mime = request.headers.get("content-type", "audio/wav")
    return await mgr.test_profile(id, audio, mime)


def _sse(event: str | None, data: dict) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n".encode()


@router.post("/providers/{id}/pull")
async def pull(id: UUID, body: PullBody, mgr: Mgr):
    stream = await mgr.pull(id, body.model_ref)  # validates provider kind before streaming starts

    async def events() -> AsyncIterator[bytes]:
        try:
            async for progress in stream:
                yield _sse(None, progress)
            yield _sse("done", {"status": "done"})
        except RuntimeCallError as e:
            yield _sse("error", {"detail": str(e)})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
