from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .ports import ProactiveStore

log = logging.getLogger("trama.proactive")
Send = Callable[[dict], Awaitable[None]]


def _frame(n: dict) -> dict:
    return {"type": "notification", "id": str(n["id"]), "kind": n["kind"], "text": n["text"]}


class InMemoryNotificationHub:
    """session_id -> send coroutine. Persists every notification; pushes it over WS when connected."""

    def __init__(self, store: ProactiveStore) -> None:
        self._store = store
        self._sessions: dict[str, Send] = {}
        self._tasks: set[asyncio.Task] = set()

    def register(self, session_id: str, send: Send) -> None:
        self._sessions[session_id] = send
        try:
            task = asyncio.get_running_loop().create_task(self.deliver_pending(session_id))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def unregister(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def is_connected(self, session_id: str) -> bool:
        return session_id in self._sessions

    async def _push(self, session_id: str, n: dict) -> bool:
        send = self._sessions.get(session_id)
        if send is None:
            return False
        try:
            await send(_frame(n))
            return True
        except Exception as e:  # dead socket: drop it, the notification stays pending
            log.info("notification push failed: %s", type(e).__name__)
            self._sessions.pop(session_id, None)
            return False

    async def notify(self, session_id: str, text: str, kind: str) -> str:
        nid = await self._store.insert_notification(session_id, text, kind)
        await self._push(session_id, {"id": nid, "kind": kind, "text": text})
        return nid

    async def deliver_pending(self, session_id: str) -> int:
        sent = 0
        try:
            pending = await self._store.undelivered(session_id)
        except Exception as e:
            log.warning("deliver_pending failed: %s", type(e).__name__)
            return 0
        for n in pending:
            if not await self._push(session_id, n):
                break
            sent += 1
        return sent

    async def ack(self, notification_id: str) -> None:
        await self._store.mark_delivered(notification_id)
