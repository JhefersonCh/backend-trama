from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .hub import InMemoryNotificationHub
from .ports import ProactiveStore
from .service import ProactiveService

log = logging.getLogger("trama.proactive")
BOGOTA = ZoneInfo("America/Bogota")
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 30
SLA_WARN_FRACTION = 0.8
CLOSED = ("resolved", "closed")


def bogota_time(dt: datetime) -> str:
    return dt.astimezone(BOGOTA).strftime("%H:%M")


class ProactiveWorker:
    def __init__(
        self,
        store: ProactiveStore,
        hub: InMemoryNotificationHub,
        service: ProactiveService,
        ticket_service: Any | None = None,
        *,
        interval: float = 30.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store, self.hub, self.service, self.tickets = store, hub, service, ticket_service
        self.interval, self._now = interval, clock
        self._task: asyncio.Task | None = None
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {
            "followup": self._followup,
            "sla_watch": self._sla_watch,
            "step_timer": self._step_timer,
            "outage_watch": self._noop,
            "remeasure": self._noop,
        }

    # lifecycle
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        try:
            await self.store.reset_running()
        except Exception as e:
            log.warning("proactive reset skipped: %s", type(e).__name__)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never kill the loop (DB down, etc.)
                log.warning("proactive tick failed: %s", type(e).__name__)
            await asyncio.sleep(self.interval)

    async def run_once(self) -> int:
        jobs = await self.store.claim_due(10)
        for job in jobs:
            await self._run(job)
        return len(jobs)

    async def _run(self, job: dict) -> None:
        jid = str(job["id"])
        try:
            handler = self.handlers.get(job["kind"], self._noop)
            await handler(job)
            await self.store.finish_job(jid, "done", None)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            attempts = int(job.get("attempts") or 1)
            if attempts >= MAX_ATTEMPTS:
                await self.store.finish_job(jid, "failed", err)
            else:
                retry = self._now() + timedelta(seconds=BACKOFF_SECONDS * 2**attempts)
                await self.store.finish_job(jid, "pending", err, retry)

    # handlers
    async def _noop(self, job: dict) -> None:
        return None

    async def _step_timer(self, job: dict) -> None:
        text = (job.get("payload") or {}).get("text") or "Ya pasó el tiempo que acordamos. ¿Cómo va todo?"
        if job.get("session_id"):
            await self.hub.notify(str(job["session_id"]), text, "step_timer")

    async def _followup(self, job: dict) -> None:
        sid = job.get("session_id")
        if not sid:
            return
        text = "¿Se resolvió su conexión? Responda sí o no."
        ticket = await self.store.open_ticket_for_session(str(sid))
        if ticket:
            text = f"Sobre su caso {ticket['ticket_number']}: " + text
        await self.hub.notify(str(sid), text, "followup")

    async def _sla_watch(self, job: dict) -> None:
        payload = job.get("payload") or {}
        tid = str(job.get("ticket_id") or payload.get("ticket_id") or "")
        info = await self.store.ticket_info(tid) if tid else None
        if not info or info["status"] in CLOSED or info.get("sla_due_at") is None:
            return
        now, due, created = self._now(), info["sla_due_at"], info["created_at"]
        if now >= due:
            await self._breach(tid, info)
            return
        if payload.get("stage") != "warned":
            warn_at = created + (due - created) * SLA_WARN_FRACTION
            if now < warn_at:
                await self._resched(job, tid, warn_at, "initial")
                return
            await self.store.add_ticket_event(
                tid, "sla_warning", {"sla_due_at": due.isoformat()}, "80 % del plazo consumido"
            )
        await self._resched(job, tid, due, "warned")

    async def _resched(self, job: dict, tid: str, run_at: datetime, stage: str) -> None:
        await self.service.schedule(
            "sla_watch",
            session_id=str(job["session_id"]) if job.get("session_id") else None,
            ticket_id=tid,
            run_at=run_at,
            payload={"ticket_id": tid, "stage": stage},
        )

    async def _breach(self, tid: str, info: dict) -> None:
        number = info["ticket_number"]
        await self.store.add_ticket_event(
            tid, "sla_breached", {"technician": info.get("technician_name")}, "plazo vencido"
        )
        new_name = info.get("technician_name")
        if self.tickets is not None:
            tl = await self.tickets.assign(number, None, True)
            new_name = tl.technician.name if tl.technician else None
        sid = info.get("session_id")
        if sid:
            where = f"se reasignó a {new_name}" if new_name else "sigue en cola y estamos buscando quién la atienda"
            await self.hub.notify(
                str(sid),
                f"Su caso {number} {where}. El plazo de {bogota_time(info['sla_due_at'])} venció.",
                "ticket_update",
            )
