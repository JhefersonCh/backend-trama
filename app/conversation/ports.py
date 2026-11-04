# ruff: noqa: E501
"""Ports of the conversation module. Collaborator shapes (knowledge, tickets) are duplicated on purpose:
wiring adapts the real implementations structurally."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel


class KnowledgeHit(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    content: str
    source_uri: str | None = None
    section: str | None = None
    score: float
    is_synthetic: bool = False
    chunk_index: int | None = None


class KnowledgeSearchPort(Protocol):
    async def search(
        self,
        query: str,
        top_k: int,
        filters: dict | None = None,
        on_load: Callable[[], Awaitable[None]] | None = None,
    ) -> list[KnowledgeHit]: ...


class TicketSummary(BaseModel):
    ticket_number: str
    status: str
    priority: str
    title: str
    created_at: datetime
    updated_at: datetime | None = None


class TicketPort(Protocol):
    async def create_incident_with_ticket(
        self,
        *,
        title: str,
        description: str,
        category: str,
        priority: str,
        session_id: str | None,
        evidence: list[dict],
    ) -> TicketSummary: ...

    async def get_ticket(self, ticket_number: str) -> TicketSummary | None: ...


class SettingsReader(Protocol):
    async def get(self, key: str, default: Any = None) -> Any: ...


class ConversationRepo(Protocol):
    async def create_session(self, channel: str) -> str: ...
    async def session_exists(self, session_id: str) -> bool: ...
    async def add_message(
        self, session_id: str, role: str, content: str, input_mode: str, model_profile_id: UUID | None
    ) -> str: ...
    async def update_message_content(self, message_id: str, content: str) -> None: ...
    async def recent_messages(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        """Oldest first; only user/assistant rows: {role, content}."""
        ...

    async def add_voice_turn(
        self,
        session_id: str,
        transcript: str | None,
        interrupted: bool,
        stt_ms: int | None,
        rag_ms: int | None,
        llm_first_token_ms: int | None,
        tts_first_audio_ms: int | None,
    ) -> str: ...
    async def mark_voice_turn_interrupted(self, voice_turn_id: str) -> None: ...
    async def add_turn_metrics(self, row: dict[str, Any]) -> None: ...
    async def touch_session(self, session_id: str, title_if_empty: str | None) -> None:
        """updated_at = now(); title set only while it is still NULL (auto-title from the first message)."""
        ...

    async def list_sessions(self, limit: int, include_archived: bool = False) -> list[dict[str, Any]]:
        """Newest activity first: {id, title, channel, status, archived, started_at, updated_at, last_message_at,
        message_count}."""
        ...

    async def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    async def update_session(self, session_id: str, changes: dict[str, Any]) -> bool:
        """changes: subset of {title, archived}. False when the session does not exist."""
        ...

    async def delete_session(self, session_id: str) -> bool:
        """Deletes the session with its messages and voice turns (FK cascade). False when absent."""
        ...

    async def list_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        """All roles incl. tool rows, oldest first; None when the session does not exist."""
        ...

    async def get_media(self, media_id: str, session_id: str) -> dict[str, Any] | None:
        """Uploaded media row {id, session_id, kind, mime, width, height, analysis, transcript} owned by the
        session; None when missing (also when the media table does not exist yet). Optional for fakes."""
        ...


class CaseTicketPort(Protocol):
    """Ticket operations the agent turn needs (TicketService satisfies it structurally)."""

    async def create_incident_with_ticket(
        self,
        *,
        title: str,
        description: str,
        category: str | None,
        priority: str,
        session_id: str | None,
        evidence: list[dict],
        zone: str | None = None,
        service_mode: str | None = None,
        preferred_slot: str | None = None,
        handoff_summary: dict | None = None,
        auto_assign: bool = True,
    ) -> Any: ...
    async def mine(self, session_id: str) -> Any:
        """TicketTimeline of the session's ticket (else the reporter's latest); raises NotFoundError."""
        ...

    async def timeline(self, ticket_number: str) -> Any: ...
    async def set_contact(self, ticket_number: str, value: str) -> Any: ...


# ------------------------------------------------------------------ proactivity (implemented in app.proactive)
Send = Callable[[dict], Awaitable[None]]


class NotificationHub(Protocol):
    def register(self, session_id: str, send: Send) -> None: ...
    def unregister(self, session_id: str) -> None: ...


class ProactivePort(Protocol):
    """Set as `app.state.proactive` by main.py's lifespan; the conversation falls back to NoopProactive."""

    hub: NotificationHub

    async def schedule(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        ticket_id: str | None = None,
        run_at: datetime,
        payload: dict | None = None,
    ) -> str: ...
    async def ack(self, notification_id: str) -> None: ...


class _NoopHub:
    def register(self, session_id: str, send: Send) -> None:
        return None

    def unregister(self, session_id: str) -> None:
        return None


class NoopProactive:
    hub: NotificationHub = _NoopHub()

    async def schedule(self, kind: str, **kw: Any) -> str:
        return ""

    async def ack(self, notification_id: str) -> None:
        return None
