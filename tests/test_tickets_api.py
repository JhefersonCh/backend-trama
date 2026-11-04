import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.tickets.deps import get_ticket_service
from app.tickets.service import TicketService
from app.tickets.wiring import router
from tests.fakes_technicians import FakeUoW, Store

BODY = {
    "title": "Sin Internet en sala 3",
    "description": "No navega",
    "category": "connectivity",
    "priority": "high",
    "evidence": [{"check": "ping", "result": "timeout"}],
}


@pytest.fixture
async def tc():
    store = Store()
    svc = TicketService(lambda: FakeUoW(store))
    app = create_app()
    app.include_router(router)  # idempotent if main.py already wires it
    app.dependency_overrides[get_ticket_service] = lambda: svc
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_create_ticket_and_get_detail_with_created_event(tc):
    r = await tc.post("/api/tickets", json=BODY)
    assert r.status_code == 201 and r.json()["ticket_number"].startswith("TRM-")
    d = (await tc.get(f"/api/tickets/{r.json()['ticket_number']}")).json()
    assert d["status"] == "open" and "ping" in d["description"]
    assert d["events"][0]["event_type"] == "created"
    assert (await tc.get("/api/tickets/TRM-999999")).status_code == 404


async def test_patch_status_writes_event(tc):
    n = (await tc.post("/api/tickets", json=BODY)).json()["ticket_number"]
    r = await tc.patch(f"/api/tickets/{n}", json={"status": "in_progress"})
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    ev = r.json()["events"][-1]
    assert ev["event_type"] == "status_changed" and ev["old_value"] == "open" and ev["new_value"] == "in_progress"


async def test_patch_invalid_status_422(tc):
    n = (await tc.post("/api/tickets", json=BODY)).json()["ticket_number"]
    assert (await tc.patch(f"/api/tickets/{n}", json={"status": "bogus"})).status_code == 422
