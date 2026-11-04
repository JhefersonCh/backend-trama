"""GET /api/cases/{session_id}/report.html|report.pdf (same prefix as the agent's cases router)."""

import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.requests import HTTPConnection
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.agent.repo import PgAgentRepo
from app.agent.service import AgentService
from app.tickets.deps import build_ticket_service

from .repo import PgReportRepo
from .service import ReportService

router = APIRouter(prefix="/api/cases", tags=["reports"])
Kind = Annotated[Literal["informe", "guia"], Query()]


def get_report_service(conn: HTTPConnection) -> ReportService:
    app = conn.app
    return ReportService(
        AgentService(PgAgentRepo(app.state.pool)),
        build_ticket_service(app),
        PgReportRepo(app.state.pool),
        Path(os.environ.get("MEDIA_DIR", "/data/media")),
    )


Svc = Annotated[ReportService, Depends(get_report_service)]


@router.get("/{session_id}/report.html", response_class=HTMLResponse)
async def report_html(session_id: UUID, svc: Svc, kind: Kind = "informe") -> HTMLResponse:
    return HTMLResponse(await svc.html(session_id, kind), headers={"Cache-Control": "no-store"})


@router.get("/{session_id}/report.pdf")
async def report_pdf(session_id: UUID, svc: Svc, kind: Kind = "informe") -> Response:
    try:
        pdf = await svc.pdf(session_id, kind)
    except (ImportError, OSError):
        return JSONResponse({"detail": "pdf no disponible; use report.html"}, status_code=501)
    name = f"trama-{'guia' if kind == 'guia' else 'informe'}-{str(session_id)[:8]}.pdf"
    return Response(
        pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )
