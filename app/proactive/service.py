from __future__ import annotations

from datetime import datetime

from app.config.errors import DomainValidationError

from .hub import InMemoryNotificationHub
from .ports import KINDS, ProactiveStore


class ProactiveService:
    def __init__(self, store: ProactiveStore, hub: InMemoryNotificationHub) -> None:
        self.store = store
        self.hub = hub

    async def schedule(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        ticket_id: str | None = None,
        run_at: datetime,
        payload: dict | None = None,
    ) -> str:
        if kind not in KINDS:
            raise DomainValidationError(f"kind must be one of {list(KINDS)}")
        if run_at.tzinfo is None:
            raise DomainValidationError("run_at must be timezone-aware")
        return await self.store.insert_job(kind, session_id, ticket_id, run_at, payload or {})

    async def ack(self, notification_id: str) -> None:
        await self.hub.ack(notification_id)
