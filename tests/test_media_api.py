# ruff: noqa: E501
import io
import wave
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.main import create_app
from app.media.router import get_media_service
from app.media.service import MediaService
from app.media.vision import extract_json, normalize

SID = uuid4()


class FakeMediaRepo:
    def __init__(self):
        self.rows: dict[UUID, dict] = {}

    async def session_exists(self, sid):
        return sid == SID

    async def insert(self, row):
        self.rows[row["id"]] = {**row, "created_at": datetime.now(UTC)}

    async def get(self, media_id):
        return self.rows.get(media_id)


class FakeStt:
    kind = "voice_service"

    async def transcribe(self, model, audio, mime, language=None, params=None):
        return "  la luz del módem está roja  "


class FakeManager:
    """Only what MediaService touches: _store.get_active/get_provider and use(role)."""

    def __init__(self, vision=None):
        self.vision = vision
        profile = SimpleNamespace(model_ref="stt", params={"language": "es"}, id=uuid4(), provider_id=uuid4())
        self._stt = SimpleNamespace(adapter=FakeStt(), profile=profile)
        self._store = self

    async def get_active(self, role):
        return self.vision if role == "vision" else None

    async def get_provider(self, pid):
        return SimpleNamespace(kind="llamacpp")

    @asynccontextmanager
    async def use(self, role):
        yield self._stt


@pytest.fixture
async def mc(tmp_path):
    app = create_app()
    repo = FakeMediaRepo()
    svc = MediaService(repo, FakeManager(), tmp_path)
    app.dependency_overrides[get_media_service] = lambda: svc
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.repo, c.root = repo, tmp_path  # type: ignore[attr-defined]
        yield c


def _jpeg_with_exif() -> bytes:
    im = Image.new("RGB", (3000, 1000), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "ACME-CAM"  # Make
    exif[0x0112] = 6  # Orientation: rotate 90
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buf.getvalue()


async def _post(c, data: bytes, name: str, ctype: str, sid=SID):
    return await c.post("/api/media", data={"session_id": str(sid)}, files={"file": (name, data, ctype)})


async def test_image_upload_strips_exif_and_rejects_bad_input(mc):
    r = await _post(mc, _jpeg_with_exif(), "foto.jpg", "image/jpeg")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "image" and body["analysis"] is None and body["vision_available"] is False
    stored = next(mc.root.rglob("*.jpg"))
    with Image.open(stored) as im:
        assert len(im.getexif()) == 0 and "exif" not in im.info
        assert max(im.size) == 2048  # capped
        assert im.height > im.width  # EXIF rotation was baked into the pixels
    assert (await _post(mc, b"hello world", "x.txt", "text/plain")).status_code == 415
    assert (await _post(mc, b"\x89PNG not really", "x.png", "image/png")).status_code == 415
    assert (await _post(mc, b"\xff\xd8" + b"0" * (8 * 1024 * 1024), "big.jpg", "image/jpeg")).status_code == 413
    assert (await _post(mc, _jpeg_with_exif(), "f.jpg", "image/jpeg", sid=uuid4())).status_code == 404


async def test_audio_upload_returns_transcript_and_enforces_length(mc):
    r = await _post(mc, _wav(1), "nota.wav", "audio/wav")
    assert r.status_code == 200 and r.json()["transcript"] == "la luz del módem está roja"
    assert r.json()["kind"] == "audio" and r.json()["analysis"] is None
    assert (await _post(mc, _wav(200), "largo.wav", "audio/wav")).status_code == 413


def test_vision_normalizes_messy_model_json():
    raw = extract_json(
        'Claro:\n```json\n{"device":"Módem","leds":[{"label":"Alarma LOS","color":"Red","blink":"true",'
        '"bbox":[120,80,40,40]},{"label":"wan","color":"Ámbar","blink":false,"bbox":[-0.2,0.5,2,0.9]},'
        '{"label":"power","color":"morado"}],"error_text":"null","confidence":85}\n```'
    )
    a = normalize(raw, (1000, 1000))
    assert a["device"] == "modem" and a["confidence"] == 0.85 and a["error_text"] is None
    assert [(x["label"], x["color"], x["blink"]) for x in a["leds"]] == [
        ("otro", "roja", True),
        ("internet", "naranja", False),
    ]
    assert a["leds"][0]["bbox"] == [0.12, 0.08, 0.04, 0.04]  # 0..1000 scale -> relative
    x, y, w, h = a["leds"][1]["bbox"]
    assert 0 <= x <= 1 and 0 <= y <= 1 and x + w <= 1.0001 and y + h <= 1.0001  # clamped


async def test_media_get_only_for_owner_session(mc):
    mid = (await _post(mc, _jpeg_with_exif(), "foto.jpg", "image/jpeg")).json()["media_id"]
    ok = await mc.get(f"/api/media/{mid}", params={"session_id": str(SID)})
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    assert (await mc.get(f"/api/media/{mid}", params={"session_id": str(uuid4())})).status_code == 404
    assert (await mc.get(f"/api/media/{mid}")).status_code == 422
