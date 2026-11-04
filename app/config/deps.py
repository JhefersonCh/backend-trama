from fastapi import Request

from .pg_repos import PgUnitOfWork
from .service import ConfigService


def get_config_service(request: Request) -> ConfigService:
    pool = request.app.state.pool
    return ConfigService(lambda: PgUnitOfWork(pool), request.app.state.cipher)
