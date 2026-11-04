"""Composition root of the conversation module (the app's main.py calls build_conversation_service)."""

from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.agent.repo import PgAgentRepo
from app.agent.service import AgentService
from app.config.pg_repos import PgSettings
from app.tools.builtin import build_registry
from app.tools.pg import PgDiagnostics, PgInventory, PgToolAudit

from .pg_repo import PgConversationRepo
from .ports import CaseTicketPort, KnowledgeSearchPort, ProactivePort, SettingsReader, TicketPort
from .router import router
from .service import ConversationService

__all__ = ["PgSettingsReader", "build_conversation_service", "router"]


class PgSettingsReader:
    """Reads app_settings on every call so UI changes apply to the next turn without restarts."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._pool.connection() as c:
            v = await PgSettings(c).get(key)
        return default if v is None else v


def build_conversation_service(
    pool: AsyncConnectionPool,
    manager: Any,
    knowledge_search: KnowledgeSearchPort,
    ticket_service: TicketPort,
    settings_reader: SettingsReader,
    proactive: ProactivePort | None = None,
) -> ConversationService:
    tools = build_registry(
        knowledge=knowledge_search,
        tickets=ticket_service,
        inventory=PgInventory(pool),
        diagnostics=PgDiagnostics(pool),
        audit=PgToolAudit(pool),
    )
    case_tickets: CaseTicketPort | None = ticket_service if hasattr(ticket_service, "mine") else None  # type: ignore[assignment]
    return ConversationService(
        PgConversationRepo(pool), manager, tools, settings_reader, knowledge_search,
        agent=AgentService(PgAgentRepo(pool)), tickets=case_tickets, proactive=proactive,
    )  # fmt: skip
