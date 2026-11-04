from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceOut(BaseModel):
    title: str
    source_uri: str | None = None
    section: str | None = None
    score: float | None = None
    is_synthetic: bool = False
    document_id: str | None = None
    chunk_id: str | None = None
    chunk_index: int | None = None
    excerpt: str | None = None


class SessionOut(BaseModel):
    id: str
    title: str | None = None
    channel: str
    status: str = "active"
    archived: bool = False
    started_at: datetime
    updated_at: datetime | None = None
    last_message_at: datetime | None = None
    message_count: int = 0


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


class MessageOut(BaseModel):
    """History entry. role 'ui' (contract §1): id = component id, content = fallback_text, plus component/props."""

    id: str
    role: str
    content: str
    input_mode: str
    created_at: datetime
    sources: list[SourceOut] | None = None
    component: str | None = None
    props: dict[str, Any] | None = None
    fallback_text: str | None = None


def as_source(d: dict[str, Any]) -> SourceOut:
    return SourceOut(**{k: d.get(k) for k in SourceOut.model_fields if k in d})
