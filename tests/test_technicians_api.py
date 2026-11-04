from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.shared.crypto import SecretCipher
from app.technicians.deps import get_technician_service
from app.technicians.service import TechnicianService
from app.tickets.deps import get_ticket_service
from app.tickets.service import TicketService
from tests.fakes_technicians import FakeUoW, Store

# Tuesday 10:00 Bogota = 15:00 UTC: inside every demo schedule.
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
SETTINGS = {"assignment_strategy": "menor_carga", "sla_hours": {"critical": 4, "high": 8, "medium": 24, "low": 72}}
TECH = {"name": "Laura Mejía", "specialties": ["fibra"], "zones": ["mocoa"], "level": "n2", "max_open_tickets": 3}
TICKET = {
    "title": "Sin internet",
    "description": "luz roja",
    "category": "fibra",
    "zone": "mocoa",
    "priority": "medium",
}


@pytest.fixture
def store():
    return Store(dict(SETTINGS), NOW)


@pytest.fixture
def cipher():
    return SecretCipher(Fernet.generate_key())


@pytest.fixture
async def c(store, cipher):
    app = create_app()
    tickets = TicketService(lambda: FakeUoW(store), cipher, lambda: NOW)
    app.dependency_overrides[get_ticket_service] = lambda: tickets
    app.dependency_overrides[get_technician_service] = lambda: TechnicianService(lambda: FakeUoW(store))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


async def mk_tech(c, **kw):
    r = await c.post("/api/technicians", json={**TECH, **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def test_technicians_crud_and_workload(c):
    t = await mk_tech(c)
    assert t["specialties"] == ["fibra"] and t["active"] is True and t["is_active"] is True and t["open_tickets"] == 0
    r = await c.patch(f"/api/technicians/{t['id']}", json={"zones": ["mocoa", "sibundoy"], "level": "n1"})
    assert r.json()["zones"] == ["mocoa", "sibundoy"] and r.json()["level"] == "n1"
    assert (await c.patch(f"/api/technicians/{t['id']}", json={"level": "n9"})).status_code == 422
    assert (await c.get(f"/api/technicians/{t['id']}/workload")).json()["open_tickets"] == 0
    assert (await c.delete(f"/api/technicians/{t['id']}")).json()["active"] is False
    assert (await c.get("/api/technicians?active_only=true")).json() == []
    assert (await c.get(f"/api/technicians/{uuid4()}/workload")).status_code == 404


async def test_auto_assign_picks_specialty_zone_and_least_load_else_queue(c):
    wifi = await mk_tech(c, name="Carlos", specialties=["wifi"], zones=["puerto_asis"])
    busy = await mk_tech(c, name="Busy", level="n1")
    free = await mk_tech(c, name="Free", level="n1")
    await c.post("/api/technicians", json={**TECH, "name": "Away", "availability": "vacaciones"})
    # 'busy' already carries one ticket (manual assignment), 'free' none -> auto picks 'free'
    first = (await c.post("/api/tickets", json={**TICKET, "title": "uno"})).json()
    assert first["status"] == "assigned" and first["queued"] is False
    await c.post(f"/api/tickets/{first['ticket_number']}/assign", json={"technician_id": busy["id"]})
    second = (await c.post("/api/tickets", json={**TICKET, "title": "dos"})).json()
    assert second["technician_name"] == "Free" and "especialidad fibra" in second["assignment_reason"]
    tl = (await c.get(f"/api/tickets/{second['ticket_number']}/timeline")).json()
    assert [e["event_type"] for e in tl["events"]] == ["created", "assigned"] and tl["technician"]["id"] == free["id"]
    # wifi technician never gets a fibra ticket; ticket in another zone with specialty nobody covers -> queue
    nobody = (await c.post("/api/tickets", json={**TICKET, "category": "wifi", "zone": "mocoa"})).json()
    assert nobody["queued"] is True and nobody["status"] == "open" and wifi["id"]
    assert [t["ticket_number"] for t in (await c.get("/api/tickets?unassigned=true")).json()] == [
        nobody["ticket_number"]
    ]
    auto = await c.post(f"/api/tickets/{nobody['ticket_number']}/assign", json={"auto": True})
    assert auto.json()["queued"] is True
    assert (await c.post(f"/api/tickets/{nobody['ticket_number']}/assign", json={})).status_code == 422


async def test_timeline_shape_and_mine(c):
    t = await mk_tech(c)
    sid = str(uuid4())
    body = {**TICKET, "session_id": sid, "priority": "high", "handoff_summary": {"sintomas": ["sin internet"]}}
    n = (await c.post("/api/tickets", json=body)).json()["ticket_number"]
    tl = (await c.get(f"/api/tickets/{n}/timeline")).json()
    assert set(tl) >= {"technician", "sla_due_at", "sla_remaining_seconds", "handoff_summary", "age_seconds",
                       "last_note", "events", "has_contact", "queued", "opened_at", "session_id"}  # fmt: skip
    assert tl["technician"] == {"id": t["id"], "name": "Laura Mejía", "specialties": ["fibra"]}
    assert 0 < tl["sla_remaining_seconds"] <= 8 * 3600 and tl["handoff_summary"] == {"sintomas": ["sin internet"]}
    mine = (await c.get(f"/api/tickets/mine?session_id={sid}")).json()
    assert mine["ticket_number"] == n
    assert (await c.get(f"/api/tickets/mine?session_id={uuid4()}")).status_code == 404
    assert (await c.get(f"/api/tickets?technician_id={t['id']}")).json()[0]["ticket_number"] == n


async def test_notes_create_event_and_notify_session(c, store):
    t = await mk_tech(c)
    sid = str(uuid4())
    n = (await c.post("/api/tickets", json={**TICKET, "session_id": sid})).json()["ticket_number"]
    r = await c.post(f"/api/tickets/{n}/notes", json={"technician_id": t["id"], "text": "Voy mañana a primera hora"})
    assert r.status_code == 201
    assert r.json()["last_note"]["text"] == "Voy mañana a primera hora"
    assert r.json()["events"][-1]["event_type"] == "technician_note"
    assert len(store.notifications) == 1 and "Laura dejó una nota" in store.notifications[0][1]
    assert (
        await c.post(f"/api/tickets/{n}/notes", json={"technician_id": str(uuid4()), "text": "x"})
    ).status_code == 404


async def test_contact_is_encrypted_and_never_returned(c, store, cipher):
    n = (await c.post("/api/tickets", json=TICKET)).json()["ticket_number"]
    assert (await c.put(f"/api/tickets/{n}/contact", json={"value": "3101234567", "consent": False})).status_code == 422
    r = await c.put(f"/api/tickets/{n}/contact", json={"value": "3101234567", "consent": True})
    assert r.status_code == 200 and r.json()["has_contact"] is True
    assert store.contacts[n] != b"3101234567" and cipher.decrypt(store.contacts[n]) == "3101234567"
    for path in (f"/api/tickets/{n}", f"/api/tickets/{n}/timeline", "/api/tickets"):
        assert "3101234567" not in (await c.get(path)).text
    assert "3101234567" not in r.text
