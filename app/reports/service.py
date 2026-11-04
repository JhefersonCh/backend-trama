# ruff: noqa: E501
"""Case report: gathers the case (facts, hypotheses, steps, ticket, sources) and renders self-contained HTML/PDF.

Privacy: no contact data is ever read here (tickets expose only `has_contact`), and the customer report leaves out
internal technician notes. `guia` renders only the step-by-step guide.
"""

import asyncio
import base64
import io
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.agent.service import AgentService
from app.config.errors import NotFoundError

from .repo import ReportRepo, parse_sources
from .topology import render_topology

log = logging.getLogger("trama.reports")
TZ = ZoneInfo("America/Bogota")
_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]),
)  # fmt: skip

SOURCE_LABEL = {
    "user_reported": "Informado por usted", "browser_measured": "Medido desde el navegador",
    "local_agent": "Agente local", "router": "Router", "public_api": "Consulta pública",
    "photo": "Foto enviada", "technician": "Técnico", "system": "Sistema TRAMA",
}  # fmt: skip
STATE_LABEL = {
    "nuevo": "Nuevo", "entendiendo": "Entendiendo el problema", "diagnosticando": "Diagnosticando",
    "intentando_solucion": "Probando una solución", "verificando": "Verificando", "resuelto": "Resuelto",
    "escalado": "Escalado a un técnico", "seguimiento": "En seguimiento", "cerrado": "Cerrado",
}  # fmt: skip
STATUS_LABEL = {"viva": "en evaluación", "aceptada": "probable", "descartada": "descartada"}
TICKET_STATUS = {"open": "Abierto", "in_progress": "En curso", "resolved": "Resuelto", "closed": "Cerrado"}
PRIORITY = {"low": "Baja", "medium": "Media", "high": "Alta", "critical": "Crítica"}
ASK_LABEL = {
    "ask_otros_equipos": "Se preguntó si otros equipos navegan",
    "ask_luz_modem": "Se preguntó por la luz del módem",
    "ask_luz_tras_reinicio": "Se preguntó por la luz del módem tras reiniciar",
    "ask_contrasena": "Se preguntó si aparece un error de contraseña",
}  # fmt: skip
RECOMMEND = {
    "H1_caida_proveedor": "Todo indica una falla en la línea o del proveedor. Mientras se resuelve, no hace falta cambiar nada en sus equipos; si la luz del módem sigue roja, un técnico revisará la línea.",
    "H2_solo_dispositivo": "El problema parece estar solo en el equipo afectado. Repita los pasos de la guía (renovar la dirección de red) y, si persiste, reinicie el equipo.",
    "H3_dns": "La conexión llega, pero fallan los nombres de las páginas (DNS). Cambie el DNS del equipo a uno público como se indica en la guía.",
    "H4_wifi": "La señal Wi-Fi es débil. Acérquese al router o use la red de 5 GHz si existe.",
    "H5_autenticacion": "Parece un problema con la clave del Wi-Fi. Olvide la red y vuelva a conectarse con cuidado de mayúsculas y minúsculas.",
}  # fmt: skip
GENERIC_STEPS = ["guide_reinicio"]


def _guide_text() -> dict[str, str]:
    try:  # the conversation module owns the step wording; reuse it, never fail the report if it moves
        from app.conversation.agent_policy import GUIDE_TEXT

        return dict(GUIDE_TEXT)
    except Exception:  # pragma: no cover
        return {}


def _local(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%d/%m/%Y %H:%M") if dt else ""


def _sentence(text: str) -> str:
    t = text.strip()
    return t[:1].upper() + t[1:] if t else t


def _steps_of(text: str) -> list[str]:
    return [_sentence(p) for p in text.replace("; luego", ";").split(";") if p.strip()]


class ReportService:
    def __init__(self, agent: AgentService, tickets: Any, repo: ReportRepo, media_root: Path | None = None) -> None:
        self.agent, self.tickets, self.repo, self.media_root = agent, tickets, repo, media_root

    async def _ticket(self, sid: UUID) -> Any | None:
        try:
            return await self.tickets.mine(str(sid))
        except NotFoundError:
            return None
        except Exception as e:  # a ticket lookup problem must not break the report
            log.warning("report: ticket lookup failed: %s", type(e).__name__)
            return None

    async def _thumb(self, row: dict[str, Any]) -> str | None:
        if not self.media_root:
            return None
        try:
            from PIL import Image

            def build() -> str:
                with Image.open(self.media_root / row["path"]) as im:
                    im = im.convert("RGB")
                    im.thumbnail((320, 320))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=72)
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

            return await asyncio.to_thread(build)
        except Exception:
            return None

    # ------------------------------------------------------------------ data
    async def build(self, sid: UUID, kind: str) -> dict[str, Any]:
        view = await self.agent.case_view(sid)  # 404 when the session does not exist
        session = await self.repo.session(sid) or {}
        hyps = [h for h in view["hypotheses"]]
        facts = [
            {"text": f["text"], "source": SOURCE_LABEL.get(f["source"], f["source"]), "certainty": f["certainty"]}
            for f in view["facts"]
        ]
        fact_map = {f["key"]: f["value"] for f in view["facts"]}
        taken = await self.agent.actions_taken(sid)
        ruleset = None
        try:
            ruleset = await self.agent.load_ruleset()
        except NotFoundError:
            pass
        guide_text = _guide_text()
        ticket = await self._ticket(sid)

        # --- guide steps from the plans of the live/accepted hypotheses (best first)
        steps: list[dict[str, Any]] = []
        ranked = [h for h in hyps if h["status"] != "descartada" and h["cf"] > 0]
        for h in ranked:
            for step in ruleset.plans.get(h["key"], []) if ruleset else []:
                name = step if isinstance(step, str) else None
                if name and name.startswith("guide_") and all(s["key"] != name for s in steps):
                    text = guide_text.get(name, name.removeprefix("guide_").replace("_", " "))
                    steps.append({"key": name, "title": _sentence(text.split(":")[0].split(",")[0])[:90],
                                  "items": _steps_of(text), "done": name in taken, "for": h.get("label")})  # fmt: skip
        if not steps:
            for name in GENERIC_STEPS:
                text = guide_text.get(
                    name, "reiniciar el módem: desconectarlo de la corriente, esperar 30 segundos, volver a conectarlo"
                )
                steps.append({"key": name, "title": "Reiniciar el módem", "items": _steps_of(text), "done": name in taken, "for": None})  # fmt: skip

        data: dict[str, Any] = {
            "kind": kind, "steps": steps, "case_id": str(sid)[:8].upper(), "generated": _local(datetime.now(UTC)),
            "opened": _local(session.get("started_at")), "title": session.get("title"),
        }  # fmt: skip
        if kind == "guia":
            return data

        top = ranked[0] if ranked else (hyps[0] if hyps else None)
        tried = [
            ASK_LABEL.get(
                a, _sentence(guide_text.get(a, a.replace("ask_", "").replace("guide_", "").replace("_", " ")))
            )
            for a in taken
        ]
        tried = [t if not a.startswith("guide_") else f"Se guió: {t[:1].lower() + t[1:]}" for a, t in zip(taken, tried, strict=True)]  # fmt: skip

        # --- timeline: trace + ticket + photos, newest last
        events: list[tuple[datetime, str, str]] = []
        if session.get("started_at"):
            events.append((session["started_at"], "caso", "Se abrió el caso."))
        creer: dict[str, Any] | None = None
        for t in view["trace"]:
            if t["phase"] == "creer":
                creer = t
            elif t["phase"] in ("percibir", "actuar", "verificar", "proactivo") and (
                t.get("reason") or t.get("action")
            ):
                txt = t.get("reason") or ASK_LABEL.get(t["action"], t["action"].replace(":", " ").replace("_", " "))
                events.append((t["created_at"], t["phase"], _sentence(txt)))
        if creer and top:
            events.append(
                (creer["created_at"], "creer", f"Hipótesis principal: {top['label']} (certeza {top['cf']:.0%}).")
            )
        photos = await self.repo.photos(sid)
        for p in photos:
            events.append((p["created_at"], "foto", "Se recibió una foto del equipo."))
        ticket_data = None
        if ticket:
            for e in ticket.events:
                if e.event_type in ("technician_note",) or "contact" in e.event_type:
                    continue  # internal / contact-related: never in the report
                label = {"created": "Se creó el ticket", "assigned": "Se asignó un técnico", "reassigned": "Se reasignó el técnico",
                         "unassigned_queue": "El ticket quedó en cola esperando técnico"}.get(e.event_type, _sentence(e.event_type.replace("_", " ")))  # fmt: skip
                events.append((e.created_at, "ticket", f"{label}."))
            ticket_data = {
                "number": ticket.ticket_number, "status": TICKET_STATUS.get(ticket.status, ticket.status),
                "priority": PRIORITY.get(ticket.priority, ticket.priority), "title": ticket.title,
                "opened": _local(ticket.created_at), "technician": ticket.technician.name if ticket.technician else None,
                "specialties": ticket.technician.specialties if ticket.technician else [], "queued": ticket.queued,
                "sla_due": _local(ticket.sla_due_at),
                "sla_remaining_h": round(ticket.sla_remaining_seconds / 3600, 1) if ticket.sla_remaining_seconds is not None else None,
            }  # fmt: skip
        events.sort(key=lambda e: e[0])
        timeline = [{"at": _local(at), "kind": k, "text": t} for at, k, t in events][-30:]

        # --- recommendation
        state = view["state"]
        if state in ("escalado", "seguimiento") or ticket_data:
            rec = "Un técnico dará seguimiento a su caso" + (f" (ticket {ticket_data['number']})" if ticket_data else "") + ". " + (RECOMMEND.get(top["key"], "") if top else "")  # fmt: skip
        elif state in ("resuelto", "cerrado"):
            rec = "El problema quedó resuelto. Si vuelve a ocurrir, abra un nuevo caso y mencione este número."
        else:
            rec = RECOMMEND.get(top["key"], "") if top else ""
        rec = rec.strip() or "Siga la guía paso a paso; si el problema continúa, pida que lo pase con un técnico."

        thumbs = []
        for p in photos[-2:]:
            a = p.get("analysis") or {}
            thumbs.append({"src": await self._thumb(p), "device": a.get("device"), "leds": a.get("leds") or [],
                           "error_text": a.get("error_text")})  # fmt: skip

        data.update(
            state=STATE_LABEL.get(state, state), verified=view["verified"], facts=facts,
            hypotheses=[{**h, "pct": max(0, round(h["cf"] * 100)), "status_label": STATUS_LABEL.get(h["status"], h["status"]), "cf_text": f"{h['cf']:+.2f}"} for h in hyps[:5]],  # fmt: skip
            top=top, tried=tried, timeline=timeline, ticket=ticket_data, recommendation=rec, photos=thumbs,
            sources=parse_sources(await self.repo.tool_payloads(sid)),
            topology=Markup(render_topology(fact_map)),  # noqa: S704 - generated by us, all text escaped inside
        )  # fmt: skip
        return data

    # ------------------------------------------------------------------ outputs
    async def html(self, sid: UUID, kind: str) -> str:
        return _ENV.get_template("report.html").render(**await self.build(sid, kind))

    async def pdf(self, sid: UUID, kind: str) -> bytes:
        from weasyprint import HTML  # raises ImportError/OSError when pango is missing -> router answers 501

        html = await self.html(sid, kind)
        return await asyncio.to_thread(lambda: HTML(string=html).write_pdf())
