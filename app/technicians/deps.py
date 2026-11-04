from fastapi import Request

from app.tickets.pg_repos import PgTicketUnitOfWork

from .service import TechnicianService


def get_technician_service(request: Request) -> TechnicianService:
    # The tickets unit of work also exposes `.technicians` (same connection/transaction).
    return TechnicianService(lambda: PgTicketUnitOfWork(request.app.state.pool))
