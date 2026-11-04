from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from .models import Tool, ToolCreate, ToolPatch, ToolTestBody, ToolTestResult, ToolVersion
from .repo import PgToolsRepo
from .service import RateLimitedError, RateLimiter, ToolsService


def get_tools_service(request: Request) -> ToolsService:
    app = request.app
    limiter = getattr(app.state, "tools_test_limiter", None)
    if limiter is None:
        limiter = app.state.tools_test_limiter = RateLimiter()
    return ToolsService(PgToolsRepo(app.state.pool), app.state.conversation_service.tools, limiter)


Svc = Annotated[ToolsService, Depends(get_tools_service)]

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("", response_model=list[Tool])
async def list_tools(svc: Svc):
    return await svc.list_all()


@router.post("", response_model=Tool, status_code=201)
async def create_tool(body: ToolCreate, svc: Svc):
    return await svc.create(body)


@router.patch("/{tool_id}", response_model=Tool)
async def patch_tool(tool_id: UUID, body: ToolPatch, svc: Svc):
    return await svc.patch(tool_id, body)


@router.post("/{tool_id}/toggle", response_model=Tool)
async def toggle_tool(tool_id: UUID, svc: Svc):
    return await svc.toggle(tool_id)


@router.post("/{tool_id}/test", response_model=ToolTestResult)
async def test_tool(tool_id: UUID, body: ToolTestBody, svc: Svc):
    try:
        return await svc.test(tool_id, body.args)
    except RateLimitedError as e:
        raise HTTPException(429, str(e)) from None


@router.get("/{tool_id}/versions", response_model=list[ToolVersion])
async def tool_versions(tool_id: UUID, svc: Svc):
    return await svc.versions(tool_id)
