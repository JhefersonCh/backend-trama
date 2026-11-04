import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from app.agent.router import cases_router, rules_router
from app.config.errors import ConflictError, DomainValidationError, NotFoundError
from app.config.router import router as config_router
from app.conversation.wiring import PgSettingsReader, build_conversation_service
from app.conversation.wiring import router as conversation_router
from app.insights.diagnostics import router as diagnostics_router
from app.insights.metrics import router as metrics_router
from app.knowledge.deps import build_search_service
from app.knowledge.router import router as knowledge_router
from app.media.repo import PgMediaRepo
from app.media.retention import MediaRetention
from app.media.router import media_root
from app.media.router import router as media_router
from app.models_runtime.errors import RuntimeUnavailableError
from app.models_runtime.manager import ModelManager
from app.models_runtime.pg_store import PgRuntimeStore
from app.models_runtime.router import router as runtime_router
from app.proactive.hub import InMemoryNotificationHub
from app.proactive.repo import PgProactiveStore
from app.proactive.router import router as proactive_router
from app.proactive.service import ProactiveService
from app.proactive.worker import ProactiveWorker
from app.reports.router import router as reports_router
from app.runtimes.factory import build_adapter
from app.shared.crypto import SecretCipher, load_or_create_master_key
from app.shared.db import check_db, make_pool
from app.shared.settings import Settings
from app.technicians.router import router as technicians_router
from app.tickets.deps import build_ticket_service
from app.tickets.router import router as tickets_router
from app.tools.repo import PgToolsRepo
from app.tools.router import router as tools_router

log = logging.getLogger("trama")


def settings_interval() -> float:
    try:
        return float(os.environ.get("PROACTIVE_POLL_SECONDS", "30"))
    except ValueError:
        return 30.0


async def _load_tools(registry) -> None:  # type: ignore[no-untyped-def]
    """Loads the DB tool registry; retries while the DB is not reachable yet (falls back to code tools meanwhile)."""
    for _ in range(10):
        await registry.refresh()
        if registry._rows is not None:
            return
        await asyncio.sleep(3)


DbCheck = Callable[[], Awaitable[bool]]


def get_db_check(request: Request) -> DbCheck:
    return lambda: check_db(request.app.state.pool)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.cipher = SecretCipher(load_or_create_master_key(settings.secrets_dir))
    app.state.pool = make_pool(settings.database_url)
    await app.state.pool.open(wait=False)  # never blocks/crashes if DB is down
    app.state.model_manager = ModelManager(PgRuntimeStore(app.state.pool), build_adapter, app.state.cipher.decrypt)
    app.state.conversation_service = build_conversation_service(
        app.state.pool,
        app.state.model_manager,
        knowledge_search=build_search_service(app),
        ticket_service=build_ticket_service(app),
        settings_reader=PgSettingsReader(app.state.pool),
    )
    app.state.tools_registry = app.state.conversation_service.tools
    app.state.tools_registry.attach(PgToolsRepo(app.state.pool), app.state.cipher.decrypt)
    tools_load_task = asyncio.create_task(_load_tools(app.state.tools_registry))
    proactive_store = PgProactiveStore(app.state.pool)
    proactive_hub = InMemoryNotificationHub(proactive_store)
    app.state.proactive = ProactiveService(proactive_store, proactive_hub)
    proactive_worker = ProactiveWorker(
        proactive_store, proactive_hub, app.state.proactive, build_ticket_service(app), interval=settings_interval()
    )
    proactive_worker.start()
    media_retention = MediaRetention(PgMediaRepo(app.state.pool), media_root())
    media_retention.start()
    try:
        await asyncio.wait_for(app.state.model_manager.reconcile(), timeout=10)
    except Exception as e:  # DB or runtimes down at boot must not stop the API
        log.warning("runtime reconcile at startup skipped: %s", type(e).__name__)
    warmup_task: asyncio.Task | None = None
    try:
        if (await app.state.model_manager._store.settings()).get("warmup_on_start", True) is True:
            warmup_task = asyncio.create_task(app.state.model_manager.warmup())
    except Exception as e:
        log.warning("warm-up not scheduled: %s", type(e).__name__)
    try:
        yield
    finally:
        tools_load_task.cancel()
        await proactive_worker.stop()
        await media_retention.stop()
        if warmup_task:
            warmup_task.cancel()
        await app.state.model_manager.aclose()
        await app.state.pool.close()


def _detail(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"detail": msg}, status_code=status)


def create_app() -> FastAPI:
    app = FastAPI(title="TRAMA backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:8080"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(NotFoundError)
    async def _nf(_: Request, e: NotFoundError):
        return _detail(404, str(e))

    @app.exception_handler(ConflictError)
    async def _cf(_: Request, e: ConflictError):
        return _detail(409, str(e))

    @app.exception_handler(DomainValidationError)
    async def _dv(_: Request, e: DomainValidationError):
        return _detail(422, str(e))

    @app.exception_handler(RequestValidationError)
    async def _rv(_: Request, e: RequestValidationError):
        msg = "; ".join(f"{'.'.join(str(p) for p in x['loc'] if p != 'body')}: {x['msg']}" for x in e.errors())
        return _detail(422, msg)

    @app.exception_handler(RuntimeUnavailableError)
    async def _ru(_: Request, e: RuntimeUnavailableError):
        return _detail(502, str(e))

    @app.exception_handler(OperationalError)
    @app.exception_handler(PoolTimeout)
    async def _db(_: Request, e: Exception):
        return _detail(503, "database unavailable")

    @app.get("/health", tags=["health"])
    async def health(check: Annotated[DbCheck, Depends(get_db_check)]):
        if await check():
            return {"status": "ok", "db": "ok"}
        return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)

    app.include_router(config_router)
    app.include_router(runtime_router)
    app.include_router(proactive_router)  # /api/jobs, /api/notifications, /api/knowledge/gaps (before knowledge)
    app.include_router(knowledge_router)
    app.include_router(technicians_router)
    app.include_router(tickets_router)
    app.include_router(conversation_router)  # /ws/session at root + /api/sessions
    app.include_router(metrics_router)
    app.include_router(diagnostics_router)
    app.include_router(reports_router)  # /api/cases/{id}/report.html|pdf (before cases: static suffix)
    app.include_router(media_router)
    app.include_router(cases_router)
    app.include_router(rules_router)
    app.include_router(tools_router)
    return app


app = create_app()
