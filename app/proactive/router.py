from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.requests import HTTPConnection

from .gaps import PgGapsRepo, group_gaps
from .service import ProactiveService

router = APIRouter(tags=["proactive"])
IV = {"24h": "24 hours", "7d": "7 days", "30d": "30 days"}


def get_proactive(conn: HTTPConnection) -> ProactiveService:
    return conn.app.state.proactive


def get_gaps_repo(conn: HTTPConnection) -> PgGapsRepo:
    return PgGapsRepo(conn.app.state.pool)


Svc = Annotated[ProactiveService, Depends(get_proactive)]
Gaps = Annotated[PgGapsRepo, Depends(get_gaps_repo)]


@router.get("/api/jobs")
async def list_jobs(
    svc: Svc,
    status: Literal["pending", "running", "done", "failed", "cancelled"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    return await svc.store.list_jobs(status, limit)


@router.get("/api/notifications")
async def list_notifications(
    svc: Svc, session_id: UUID, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[dict[str, Any]]:
    return await svc.store.list_notifications(str(session_id), limit)


@router.post("/api/notifications/{notification_id}/ack", status_code=204)
async def ack_notification(notification_id: UUID, svc: Svc) -> None:
    await svc.ack(str(notification_id))


@router.get("/api/knowledge/gaps")
async def knowledge_gaps(
    repo: Gaps, range_: Annotated[Literal["24h", "7d", "30d"], Query(alias="range")] = "7d"
) -> list[dict[str, Any]]:
    return group_gaps(await repo.rows(IV[range_], await repo.min_score()))
