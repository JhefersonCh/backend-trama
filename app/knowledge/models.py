from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeHit(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    content: str
    source_uri: str | None
    section: str | None
    score: float  # cosine similarity (what rag_min_score is calibrated against)
    is_synthetic: bool
    chunk_index: int | None = None
    rank_score: float | None = None  # hybrid ordering score (cosine x type weight + lexical bonus), if ranking is on


class DocumentMeta(BaseModel):
    """Metadata accepted at upload time (all optional except what the file itself provides)."""

    title: str | None = None
    publisher: str | None = None
    source_uri: str | None = None
    document_type: str | None = None
    license: str | None = None
    language: str = "es"
    version: str | None = None
    publication_date: str | None = None
    data_origin: Literal["synthetic", "lab", "real", "public_doc"] = "public_doc"
    is_synthetic: bool = False


class ChunkDraft(BaseModel):
    chunk_index: int
    content: str
    section: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    id: UUID
    title: str
    source_uri: str | None
    publisher: str | None
    version: str | None
    license: str | None
    language: str
    checksum: str | None
    document_type: str | None
    is_synthetic: bool
    data_origin: str
    ingested_at: datetime
    chunk_count: int = 0
    embedded_count: int = 0


class ChunkOut(BaseModel):
    id: UUID
    chunk_index: int
    content: str
    metadata: dict[str, Any]


class DocumentDetail(Document):
    metadata: dict[str, Any] = Field(default_factory=dict)  # publication_date, chunking params, ...
    preview: list[ChunkOut] = Field(default_factory=list)


class ContentSection(BaseModel):
    chunk_index: int
    section: str | None = None
    start: int | None = None  # char offsets of the chunk inside full_text (null when not locatable)
    end: int | None = None


class DocumentContent(BaseModel):
    id: UUID
    title: str
    source_uri: str | None
    publisher: str | None = None
    document_type: str | None
    language: str
    license: str | None
    is_synthetic: bool
    full_text: str
    sections: list[ContentSection] = Field(default_factory=list)


class IngestResult(BaseModel):
    document: Document
    chunks: int
    embedded: int
    warning: str | None = None


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    filters: dict[str, Any] | None = None
    lexical_weight: float | None = Field(default=None, ge=0, le=1)  # override rag_lexical_weight (eval/tuning)
    type_weights: dict[str, float] | None = None  # override rag_type_weights (eval/tuning)


class ReindexJob(BaseModel):
    id: UUID
    from_profile_id: UUID | None
    to_profile_id: UUID
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    progress: float
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
