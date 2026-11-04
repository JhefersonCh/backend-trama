"""Single import point for main.py and for modules that call TicketService."""

from .deps import build_ticket_service, get_ticket_service
from .router import router

__all__ = ["router", "build_ticket_service", "get_ticket_service"]
