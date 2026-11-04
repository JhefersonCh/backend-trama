from fastapi import Request

from .pg_repos import PgTicketUnitOfWork
from .service import TicketService


def build_ticket_service(app) -> TicketService:
    return TicketService(lambda: PgTicketUnitOfWork(app.state.pool), getattr(app.state, "cipher", None))


def get_ticket_service(request: Request) -> TicketService:
    return build_ticket_service(request.app)
