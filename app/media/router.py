"""REST: POST /api/media (multipart), GET /api/media/{id}?session_id=."""

import os
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.requests import HTTPConnection
from fastapi.responses import FileResponse

from .repo import PgMediaRepo
from .service import MAX_AUDIO_BYTES, MAX_IMAGE_BYTES, MediaError, MediaService

router = APIRouter(prefix="/api/media", tags=["media"])


def media_root() -> Path:
    return Path(os.environ.get("MEDIA_DIR", "/data/media"))


def get_media_service(conn: HTTPConnection) -> MediaService:
    return MediaService(PgMediaRepo(conn.app.state.pool), conn.app.state.model_manager, media_root())


Svc = Annotated[MediaService, Depends(get_media_service)]


@router.post("")
async def upload_media(
    svc: Svc,
    file: Annotated[UploadFile, File()],
    session_id: Annotated[UUID, Form()],
    consent: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    raw = await file.read(max(MAX_IMAGE_BYTES, MAX_AUDIO_BYTES) + 1)  # never buffer more than the largest limit
    try:
        return await svc.upload(session_id, raw, file.content_type, file.filename, consent)
    except MediaError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/{media_id}")
async def get_media(media_id: UUID, session_id: Annotated[UUID, Query()], svc: Svc) -> FileResponse:
    path, mime = await svc.open(media_id, session_id)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})
