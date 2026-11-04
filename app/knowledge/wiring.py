"""Single import point for main.py and for other modules that call the knowledge services."""

from .deps import build_search_service, build_service, get_knowledge_service, get_search_service
from .router import router

__all__ = ["router", "build_service", "build_search_service", "get_knowledge_service", "get_search_service"]
