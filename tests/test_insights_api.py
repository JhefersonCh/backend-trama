# ruff: noqa: E501
import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.insights import diagnostics as dg
from app.insights import metrics as mt
from app.main import create_app

NOW = datetime.now(UTC)
SID = "11111111-1111-1111-1111-111111111111"


class FakeMetrics:
    async def totals(self, iv):
        z = {f"{c}_p{p}": None for c in ("stt_ms", "tts_first_audio_ms") for p in (50, 95)}
        z |= {f"{c}_p{p}": 100.0 * p for c in ("rag_ms", "llm_first_token_ms", "total_ms") for p in (50, 95)}
        return z | dict(
            total=3,
            interrupted=1,
            prompt_avg=900.4,
            cached_avg=600.0,
            completion_avg=120.0,
            cached_sum=1800,
            prompt_sum=2700,
            with_sources=2,
            no_context=1,
            avg_top_score=0.79,
            voseo=0,
            cites=1,
            rewrites=1,
            cutoffs=0,
        )

    async def grouped(self, col, iv):
        return [{"k": "diagnostic", "n": 3}] if col == "intent" else [{"k": "text", "n": 3}]

    async def tools(self, iv):
        return [{"name": "get_ticket_status", "calls": 2, "ok": 1, "errors": 1}]

    async def model_loads(self, iv):
        return [{"profile": "lfm3-1.7b", "role": "llm", "loads": 1, "evictions": 0, "errors": 0, "avg_ms": 5000.0}]

    async def tickets(self, iv):
        return {"open": 1, "in_progress": 0, "resolved": 0, "closed": 0, "created_in_range": 1}

    async def sessions(self, iv):
        return 2


class FakeDiag:
    async def session(self, sid):
        return {"id": SID, "title": "Wifi", "started_at": NOW, "updated_at": NOW} if sid == SID else None

    async def last_turn(self, sid):
        return None

    async def messages(self, sid):
        tool = {
            "name": "search_knowledge",
            "auto": True,
            "ok": True,
            "summary": "1",
            "sources": [{"title": "Luz roja", "section": None, "score": 0.79, "is_synthetic": True}],
        }
        return [
            {"role": "user", "content": "se me fue el wifi y hay luz roja", "created_at": NOW},
            {"role": "tool", "content": json.dumps(tool), "created_at": NOW + timedelta(seconds=1)},
            {"role": "assistant", "content": "Reinicie el módem.", "created_at": NOW + timedelta(seconds=2)},
        ]

    async def observations(self, sid):
        return []

    async def tickets(self, sid):
        return []


@pytest.fixture
async def client():
    app = create_app()
    app.dependency_overrides[mt.get_metrics_repo] = lambda: FakeMetrics()
    app.dependency_overrides[dg.get_diag_repo] = lambda: FakeDiag()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_metrics_summary_shape(client):
    d = (await client.get("/api/metrics/summary?range=24h")).json()
    assert d["turns"] == {
        "total": 3,
        "by_intent": {"diagnostic": 3},
        "by_mode": {"text": 3, "voice": 0},
        "interrupted": 1,
    }
    assert d["latency_ms"]["stt"] == {"p50": None, "p95": None} and d["latency_ms"]["total"]["p95"] == 9500
    assert d["tokens"]["cache_hit_ratio"] == pytest.approx(0.6667) and d["tools"][0]["avg_ms"] is None
    assert d["model_loads"][0]["avg_ms"] == 5000 and d["sessions"] == {"in_range": 2}
    assert (await client.get("/api/metrics/summary?range=1y")).status_code == 422


async def test_diagnostic_detail_404_and_shape(client):
    assert (await client.get("/api/diagnostics/nope")).status_code == 404
    assert (await client.get("/api/diagnostics/22222222-2222-2222-2222-222222222222")).status_code == 404
    d = (await client.get(f"/api/diagnostics/{SID}")).json()
    assert d["status"] == "en_curso" and d["case_state"]["facts"][0]["value"] is True
    assert d["sources_used"][0]["times"] == 1 and [e["kind"] for e in d["timeline"]] == ["user", "sources", "assistant"]
