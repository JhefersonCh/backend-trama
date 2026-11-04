# ruff: noqa: E501
"""The TRAMA tools. None of them executes real network commands in this iteration."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.conversation.ports import KnowledgeSearchPort, TicketPort
from app.knowledge.chunking import excerpt

from .parsers import UnsupportedCommandError, parse_command_output
from .ports import AuditPort, DiagnosticsPort, InventoryPort
from .registry import ToolContext, ToolOutput, ToolRegistry, ToolSpec

Category = Literal["connectivity", "dhcp", "dns", "wifi", "ethernet", "vlan", "latency", "other"]
Priority = Literal["low", "medium", "high", "critical"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class SearchKnowledgeArgs(_Args):
    query: str = Field(min_length=2, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=20)


class TicketStatusArgs(_Args):
    ticket_number: str = Field(min_length=3, max_length=40, description="TRM-000123")


class CreateTicketArgs(_Args):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=4000)
    category: Category
    priority: Priority
    evidence: list[str] = Field(default_factory=list, max_length=20)


class InventoryArgs(_Args):
    site: str | None = Field(default=None, max_length=100)


class NetworkDetailsArgs(_Args):
    network_name_or_cidr: str = Field(min_length=2, max_length=100)


class ParseOutputArgs(_Args):
    os: Literal["windows", "linux", "macos"]
    command: str = Field(min_length=2, max_length=200, description="ipconfig /all, ip addr, ping...")
    raw_output: str = Field(min_length=1, max_length=30000)


def build_registry(
    *,
    knowledge: KnowledgeSearchPort,
    tickets: TicketPort,
    inventory: InventoryPort,
    diagnostics: DiagnosticsPort,
    audit: AuditPort,
) -> ToolRegistry:
    async def search_knowledge(a: SearchKnowledgeArgs, ctx: ToolContext) -> ToolOutput:
        hits = await knowledge.search(a.query, a.top_k or ctx.rag_top_k)
        sources = [
            {
                "title": h.title,
                "source_uri": h.source_uri,
                "section": h.section,
                "score": round(h.score, 4),
                "is_synthetic": h.is_synthetic,
                "document_id": h.document_id,
                "chunk_id": h.chunk_id,
                "chunk_index": getattr(h, "chunk_index", None),
                "excerpt": excerpt(h.content),
            }
            for h in hits
        ]
        data: dict[str, Any] = {
            "hits": [{**s, "content": h.content[:1200]} for s, h in zip(sources, hits, strict=True)],
        }
        if not hits:
            data["note"] = "Sin resultados en la base de conocimiento."
        return ToolOutput(data, f"{len(hits)} fragmento(s) encontrados", sources)

    async def get_ticket_status(a: TicketStatusArgs, ctx: ToolContext) -> ToolOutput:
        t = await tickets.get_ticket(a.ticket_number.upper())
        if t is None:
            return ToolOutput({"found": False}, f"ticket {a.ticket_number} no encontrado")
        return ToolOutput(
            {"found": True, **t.model_dump(mode="json")}, f"{t.ticket_number}: {t.status} (prioridad {t.priority})"
        )

    async def create_ticket(a: CreateTicketArgs, ctx: ToolContext) -> ToolOutput:
        # Idempotent per conversation: never open a second ticket while this session already has an open one
        # (the agent core may have created it in this same turn; an eager LLM must not duplicate it).
        mine = getattr(tickets, "mine", None)
        if mine is not None and ctx.session_id:
            try:
                cur = await mine(ctx.session_id)
            except Exception:  # NotFoundError (no ticket yet) or lookup failure -> create normally
                cur = None
            same_session = cur is not None and str(getattr(cur, "session_id", "") or "") == str(ctx.session_id)
            if same_session and getattr(cur, "status", "") not in ("resolved", "closed"):
                return ToolOutput(
                    {"created": False, "existing": True, "ticket_number": cur.ticket_number,
                     "status": cur.status, "priority": cur.priority},
                    f"ya existe el ticket {cur.ticket_number} para este caso",
                )  # fmt: skip
        t = await tickets.create_incident_with_ticket(
            title=a.title,
            description=a.description,
            category=a.category,
            priority=a.priority,
            session_id=ctx.session_id,
            evidence=[{"text": e, "origin": "conversation"} for e in a.evidence],
        )
        return ToolOutput({"created": True, **t.model_dump(mode="json")}, f"ticket {t.ticket_number} creado")

    async def get_network_inventory(a: InventoryArgs, ctx: ToolContext) -> ToolOutput:
        inv = await inventory.inventory(a.site or None)
        return ToolOutput(inv, f"{len(inv.get('devices', []))} dispositivo(s), {len(inv.get('networks', []))} red(es)")

    async def get_network_details(a: NetworkDetailsArgs, ctx: ToolContext) -> ToolOutput:
        rows = await inventory.network_details(a.network_name_or_cidr)
        if not rows:
            return ToolOutput({"found": False}, f"red {a.network_name_or_cidr!r} no encontrada")
        return ToolOutput({"found": True, "networks": rows}, f"{len(rows)} red(es) encontradas")

    async def parse_output(a: ParseOutputArgs, ctx: ToolContext) -> ToolOutput:
        try:
            p = parse_command_output(a.os, a.command, a.raw_output)
        except UnsupportedCommandError as e:
            raise ValueError(str(e)) from e
        stored = await diagnostics.record(ctx.session_id, p.observations)
        data = {
            "kind": p.kind,
            "origin": "user_provided",
            "parsed": p.parsed,
            "findings": p.findings,
            "observations_stored": stored,
        }
        return ToolOutput(data, p.summary)

    tools = [
        ToolSpec(
            "search_knowledge",
            "Busca en la base de conocimiento técnica.",
            SearchKnowledgeArgs,
            search_knowledge,
            timeout_s=30,
            phase="retrieving",
            intents=("diagnostic", "guide"),
        ),
        ToolSpec("get_ticket_status", "Estado de un ticket.", TicketStatusArgs, get_ticket_status, intents=("ticket",)),
        ToolSpec(
            "create_ticket",
            "Crea un ticket. Solo si el usuario lo confirmó.",
            CreateTicketArgs,
            create_ticket,
            timeout_s=20,
            intents=("ticket", "diagnostic", "output"),
        ),
        ToolSpec(
            "get_network_inventory",
            "Lista dispositivos y redes registrados.",
            InventoryArgs,
            get_network_inventory,
            intents=("inventory",),
        ),
        ToolSpec(
            "get_network_details",
            "Detalle de una red registrada (gateway, DNS, VLAN).",
            NetworkDetailsArgs,
            get_network_details,
            intents=("inventory",),
        ),
        ToolSpec(
            "parse_command_output",
            "Analiza una salida de comando pegada por el usuario.",
            ParseOutputArgs,
            parse_output,
            intents=("output",),
        ),
    ]
    return ToolRegistry(tools, audit, knowledge=knowledge)
