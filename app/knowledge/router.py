from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile

from app.config.errors import DomainValidationError

from .deps import get_knowledge_service, get_search_service
from .models import (
    Document,
    DocumentContent,
    DocumentDetail,
    DocumentMeta,
    IngestResult,
    KnowledgeHit,
    ReindexJob,
    SearchBody,
)
from .service import KnowledgeSearchService, KnowledgeService

Svc = Annotated[KnowledgeService, Depends(get_knowledge_service)]
Search = Annotated[KnowledgeSearchService, Depends(get_search_service)]

MAX_UPLOAD = 10 * 1024 * 1024

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.post("/documents", response_model=IngestResult, status_code=201)
async def upload_document(
    svc: Svc,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    publisher: Annotated[str | None, Form()] = None,
    source_uri: Annotated[str | None, Form()] = None,
    document_type: Annotated[str | None, Form()] = None,
    license: Annotated[str | None, Form()] = None,
    language: Annotated[str, Form()] = "es",
    version: Annotated[str | None, Form()] = None,
    publication_date: Annotated[str | None, Form()] = None,
    data_origin: Annotated[Literal["synthetic", "lab", "real", "public_doc"], Form()] = "public_doc",
    is_synthetic: Annotated[bool, Form()] = False,
):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise DomainValidationError("file too large (max 10 MB)")
    meta = DocumentMeta(
        title=title or None,
        publisher=publisher or None,
        source_uri=source_uri or None,
        document_type=document_type or None,
        license=license or None,
        language=language,
        version=version or None,
        publication_date=publication_date or None,
        data_origin=data_origin,
        is_synthetic=is_synthetic,
    )
    return await svc.ingest(file.filename or "upload.txt", data, meta)


@router.get("/documents", response_model=list[Document])
async def list_documents(svc: Svc):
    return await svc.list_documents()


@router.get("/documents/{id}", response_model=DocumentDetail)
async def get_document(id: UUID, svc: Svc):
    return await svc.get_document(id)


@router.get("/documents/{id}/content", response_model=DocumentContent)
async def get_document_content(id: UUID, svc: Svc):
    return await svc.get_content(id)


@router.delete("/documents/{id}", status_code=204)
async def delete_document(id: UUID, svc: Svc):
    await svc.delete_document(id)
    return Response(status_code=204)


@router.post("/search", response_model=list[KnowledgeHit])
async def search(body: SearchBody, svc: Search):
    return await svc.search(
        body.query, body.top_k, body.filters, lexical_weight=body.lexical_weight, type_weights=body.type_weights
    )


@router.post("/reindex", response_model=ReindexJob, status_code=202)
async def start_reindex(svc: Svc):
    return await svc.start_reindex()


@router.get("/reindex/latest", response_model=ReindexJob)
async def latest_reindex(svc: Svc):
    return await svc.latest_job()


@router.get("/reindex/{id}", response_model=ReindexJob)
async def get_reindex(id: UUID, svc: Svc):
    return await svc.get_job(id)
