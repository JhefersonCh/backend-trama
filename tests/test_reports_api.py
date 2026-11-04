# ruff: noqa: E501
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.service import AgentService
from app.main import create_app
from app.reports.router import get_report_service
from app.reports.service import ReportService
from tests.test_agent_api import SID, FakeAgentRepo


class FakeReportRepo:
    async def session(self, sid):
        return {"id": sid, "started_at": datetime.now(UTC) - timedelta(hours=2), "title": "Sin internet en casa"}

    async def tool_payloads(self, sid):
        src = {
            "title": "Guía de luces del módem",
            "section": "LOS",
            "excerpt": "Una luz LOS roja indica pérdida de señal.",
        }
        return [json.dumps({"sources": [src, src]})]

    async def photos(self, sid):
        return []


class FakeTickets:
    async def mine(self, session_id):
        ev = lambda t, d=None: SimpleNamespace(event_type=t, details=d, created_at=datetime.now(UTC))  # noqa: E731
        return SimpleNamespace(
            ticket_number="T-1001", status="open", priority="high", title="Falla de línea", queued=False,
            created_at=datetime.now(UTC), sla_due_at=datetime.now(UTC) + timedelta(hours=4), sla_remaining_seconds=14400,
            technician=SimpleNamespace(name="Ana Pérez", specialties=["fibra"]), has_contact=True,
            events=[ev("created"), ev("assigned"), ev("contact_registered", "3001234567"), ev("technician_note", "interno")],
        )  # fmt: skip


@pytest.fixture
async def rc():
    app = create_app()
    agent = AgentService(FakeAgentRepo())
    await agent.upsert_fact(SID, "otros_equipos_navegan", False, "user_reported")
    await agent.upsert_fact(SID, "luz_modem", "roja", "photo", 0.8)
    await agent.recompute(SID)
    app.dependency_overrides[get_report_service] = lambda: ReportService(agent, FakeTickets(), FakeReportRepo())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_report_html_sections_and_no_contact(rc):
    r = await rc.get(f"/api/cases/{SID}/report.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    h = r.text
    for needle in ("Informe del caso", "Caída del proveedor", "<svg", "Foto enviada", "T-1001", "Ana Pérez",
                   "Guía de luces del módem", "“Una luz LOS roja", "Línea de tiempo", "Recomendación", "@page"):  # fmt: skip
        assert needle in h, needle
    assert h.count("Una luz LOS roja") == 1  # sources deduplicated
    assert "3001234567" not in h and "contact_registered" not in h and "registrado" not in h and "interno" not in h
    g = (await rc.get(f"/api/cases/{SID}/report.html", params={"kind": "guia"})).text
    assert "Guía paso a paso" in g and "Línea de tiempo" not in g and '<svg class="logo"' in g
    assert (await rc.get(f"/api/cases/{uuid4()}/report.html")).status_code == 404
    assert (await rc.get(f"/api/cases/{SID}/report.html", params={"kind": "x"})).status_code == 422
