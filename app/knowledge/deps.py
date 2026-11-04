from fastapi import Request

from .pg_repos import PgKnowledgeRepo
from .service import KnowledgeSearchService, KnowledgeService


def build_service(app) -> KnowledgeService:
    return KnowledgeService(PgKnowledgeRepo(app.state.pool), app.state.model_manager)


def build_search_service(app) -> KnowledgeSearchService:
    return KnowledgeSearchService(PgKnowledgeRepo(app.state.pool), app.state.model_manager)


def get_knowledge_service(request: Request) -> KnowledgeService:
    return build_service(request.app)


def get_search_service(request: Request) -> KnowledgeSearchService:
    return build_search_service(request.app)
