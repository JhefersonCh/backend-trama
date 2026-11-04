from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.proactive import router as pr
from app.proactive.gaps import group_gaps
from app.proactive.hub import InMemoryNotificationHub
from app.proactive.service import ProactiveService
from app.proactive.worker import ProactiveWorker

SID = "11111111-1111-1111-1111-111111111111"
TID = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


class FakeStore:
    def __init__(self):
        self.jobs, self.notifs, self.events, self.ticket = [], [], [], None

    async def insert_job(self, kind, session_id, ticket_id, run_at, payload):
        j = {
            "id": f"j{len(self.jobs)}",
            "kind": kind,
            "session_id": session_id,
            "ticket_id": ticket_id,
            "run_at": run_at,
            "payload": payload,
            "status": "pending",
            "attempts": 1,
        }
        self.jobs.append(j)
        return j["id"]

    async def claim_due(self, limit):
        return [j for j in self.jobs if j["status"] == "pending" and j["run_at"] <= NOW]

    async def finish_job(self, job_id, status, error, run_at=None):
        next(j for j in self.jobs if j["id"] == job_id).update(status=status)

    async def reset_running(self): ...
    async def list_jobs(self, status, limit):
        return [j for j in self.jobs if status in (None, j["status"])][:limit]

    async def insert_notification(self, session_id, text, kind):
        self.notifs.append({"id": f"n{len(self.notifs)}", "kind": kind, "text": text, "session_id": session_id})
        return self.notifs[-1]["id"]

    async def undelivered(self, session_id):
        return list(self.notifs)

    async def mark_delivered(self, nid): ...
    async def list_notifications(self, session_id, limit):
        return self.notifs

    async def ticket_info(self, ticket_id):
        return self.ticket

    async def open_ticket_for_session(self, session_id):
        return {"ticket_number": "TRM-000004"}

    async def add_ticket_event(self, ticket_id, event_type, new, details):
        self.events.append(event_type)


def build(clock=lambda: NOW):
    store = FakeStore()
    hub = InMemoryNotificationHub(store)
    svc = ProactiveService(store, hub)
    return store, hub, svc, ProactiveWorker(store, hub, svc, None, clock=clock)


async def test_followup_creates_notification_and_pushes_via_hub():
    store, hub, svc, worker = build()
    sent = []

    async def send(frame):
        sent.append(frame)

    hub.register(SID, send)
    await svc.schedule("followup", session_id=SID, run_at=NOW - timedelta(seconds=1))
    assert await worker.run_once() == 1
    assert store.jobs[0]["status"] == "done"
    assert sent[0]["type"] == "notification" and "TRM-000004" in sent[0]["text"] and sent[0]["kind"] == "followup"


async def test_sla_watch_at_80_percent_warns_and_reschedules_for_due():
    created = NOW - timedelta(hours=8, minutes=30)
    store, _, svc, worker = build()
    store.ticket = {
        "ticket_number": "TRM-000004",
        "status": "assigned",
        "created_at": created,
        "sla_due_at": created + timedelta(hours=10),
        "session_id": SID,
        "technician_name": "Ana",
    }
    await svc.schedule("sla_watch", ticket_id=TID, run_at=NOW - timedelta(seconds=1), payload={"ticket_id": TID})
    await worker.run_once()
    assert store.events == ["sla_warning"]
    assert store.jobs[1]["run_at"] == created + timedelta(hours=10) and store.jobs[1]["payload"]["stage"] == "warned"


def test_gaps_group_similar_queries():
    t = NOW
    out = group_gaps(
        [
            {"text": "No me funciona el internet del módem", "at": t},
            {"text": "el módem no funciona, sin internet", "at": t - timedelta(hours=1)},
            {"text": "Cómo configuro la VPN", "at": t - timedelta(hours=2)},
        ]
    )
    assert [g["count"] for g in out] == [2, 1] and len(out[0]["examples"]) == 2


@pytest.fixture
async def client():
    app = create_app()
    store, _, svc, _ = build()
    await svc.schedule("followup", session_id=SID, run_at=NOW)
    app.dependency_overrides[pr.get_proactive] = lambda: svc
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_jobs_endpoint(client):
    r = await client.get("/api/jobs", params={"status": "pending"})
    assert r.status_code == 200 and r.json()[0]["kind"] == "followup"
    assert (await client.get("/api/jobs", params={"status": "bogus"})).status_code == 422
