# ruff: noqa: E501
"""Media use cases: validate -> clean/store -> (transcribe | classify) -> persist."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.config.errors import NotFoundError
from app.models_runtime.manager import ModelManager

from . import audio as audio_mod
from . import vision
from .images import ImageError, clean_image
from .repo import MediaRepo

log = logging.getLogger("trama.media")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024
IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


class MediaError(Exception):
    """Request problem with an HTTP status to report (413 size, 415 type, 422 content)."""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


def is_external(profile: Any, provider: Any) -> bool:
    """A vision model that sends the photo off the laptop: no local RAM cost or a hosted OpenAI-compatible API."""
    return (profile.est_memory_mb or 0) == 0 or getattr(provider, "kind", None) == "openai_compatible"


class MediaService:
    def __init__(self, repo: MediaRepo, manager: ModelManager | Any, root: Path) -> None:
        self.repo, self.manager, self.root = repo, manager, Path(root)

    # ------------------------------------------------------------------ vision profile
    async def _vision(self) -> tuple[Any, Any] | None:
        store = self.manager._store
        profile = await store.get_active("vision")
        if profile is None:
            return None
        return profile, await store.get_provider(profile.provider_id)

    # ------------------------------------------------------------------ upload
    async def upload(
        self, session_id: UUID, raw: bytes, content_type: str | None, filename: str | None, consent: bool
    ) -> dict[str, Any]:
        if not await self.repo.session_exists(session_id):
            raise NotFoundError("session not found")
        ct = (content_type or "").split(";")[0].strip().lower()
        sniffed = audio_mod.sniff(raw)
        if ct in IMAGE_MIMES or (not ct.startswith("audio/") and sniffed is None):
            return await self._image(session_id, raw, consent)
        if sniffed is not None:
            return await self._audio(session_id, raw, sniffed)
        raise MediaError(415, "unsupported file type (images: JPG/PNG/WebP, audio: webm/ogg/mp3/wav/m4a)")

    def _store(self, session_id: UUID, media_id: UUID, ext: str, data: bytes) -> str:
        rel = f"{session_id}/{media_id}.{ext}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return rel

    async def _image(self, session_id: UUID, raw: bytes, consent: bool) -> dict[str, Any]:
        if len(raw) > MAX_IMAGE_BYTES:
            raise MediaError(413, "image too large (max 8 MB)")
        try:
            img = await asyncio.to_thread(clean_image, raw)
        except ImageError as e:
            raise MediaError(415, str(e)) from e
        mid = uuid4()
        rel = await asyncio.to_thread(self._store, session_id, mid, "jpg", img.data)
        found = await self._vision()
        out: dict[str, Any] = {
            "media_id": str(mid), "kind": "image", "url": f"/api/media/{mid}", "mime": img.mime, "transcript": None,
            "analysis": None, "vision_available": found is not None, "consent_required": False,
            "width": img.width, "height": img.height,
        }  # fmt: skip
        analysis: dict[str, Any] | None = None
        if found is not None:
            profile, provider = found
            if is_external(profile, provider) and not consent:
                out["consent_required"] = True
            else:
                analysis, detail = await self._classify(img.data, (img.width, img.height))
                out["analysis"] = analysis
                if detail:
                    out["detail"] = detail
        await self.repo.insert({
            "id": mid, "session_id": session_id, "kind": "image", "mime": img.mime, "path": rel,
            "size_bytes": len(img.data), "width": img.width, "height": img.height, "analysis": analysis,
        })  # fmt: skip
        return out

    async def _classify(self, jpeg: bytes, size: tuple[int, int]) -> tuple[dict[str, Any] | None, str | None]:
        t0 = time.perf_counter()
        try:
            async with self.manager.use("vision") as lease:
                analysis = await vision.classify(lease, jpeg, size)
            log.info("vision ok in %d ms", (time.perf_counter() - t0) * 1000)
            return analysis, None
        except Exception as e:  # analysis is best-effort: the photo is already stored and attachable
            log.warning("vision failed: %s: %s", type(e).__name__, e)
            return None, f"no se pudo analizar la foto ({type(e).__name__})"

    async def _audio(self, session_id: UUID, raw: bytes, sniffed: tuple[str, str]) -> dict[str, Any]:
        mime, ext = sniffed
        if len(raw) > MAX_AUDIO_BYTES:
            raise MediaError(413, "audio too large (max 10 MB)")
        secs = audio_mod.duration_seconds(raw, mime)
        if secs is not None and secs > audio_mod.MAX_SECONDS:
            raise MediaError(413, "audio too long (max 3 minutes)")
        mid = uuid4()
        rel = await asyncio.to_thread(self._store, session_id, mid, ext, raw)
        transcript: str | None = None
        detail: str | None = None
        try:
            async with self.manager.use("stt") as lease:
                p = lease.profile.params
                transcript = (
                    await lease.adapter.transcribe(lease.profile.model_ref, raw, mime, p.get("language"), p)
                ).strip()
        except Exception as e:
            log.warning("transcription failed: %s", type(e).__name__)
            detail = "el servicio de voz no está disponible; no se pudo transcribir"
        await self.repo.insert({
            "id": mid, "session_id": session_id, "kind": "audio", "mime": mime, "path": rel, "size_bytes": len(raw),
            "transcript": transcript,
        })  # fmt: skip
        out: dict[str, Any] = {
            "media_id": str(mid), "kind": "audio", "url": f"/api/media/{mid}", "mime": mime,
            "transcript": transcript, "analysis": None, "vision_available": False, "consent_required": False,
        }  # fmt: skip
        if detail:
            out["detail"] = detail
        return out

    # ------------------------------------------------------------------ read
    async def open(self, media_id: UUID, session_id: UUID) -> tuple[Path, str]:
        row = await self.repo.get(media_id)
        if row is None or row["session_id"] != session_id:  # same answer for both: do not reveal other sessions' ids
            raise NotFoundError("media not found")
        path = (self.root / row["path"]).resolve()
        if self.root.resolve() not in path.parents or not path.is_file():
            raise NotFoundError("media not found")
        return path, row["mime"]
