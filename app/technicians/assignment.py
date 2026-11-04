"""Pure assignment decision (spec 4.2 K2). No I/O: the caller loads data and persists the outcome."""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

BOGOTA = ZoneInfo("America/Bogota")
STRATEGIES = ("menor_carga", "round_robin", "manual")
CONTINUITY_DAYS = 30
_DAYS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]

# ticket.category -> technician specialty. Unknown categories skip the specialty filter.
CATEGORY_SPECIALTY: dict[str, str] = {
    "connectivity": "redes", "conectividad": "redes", "network": "redes", "red": "redes", "redes": "redes",
    "dns": "redes", "dhcp": "redes", "vlan": "redes", "routing": "redes", "internet": "fibra",
    "fibra": "fibra", "fiber": "fibra", "fibra_optica": "fibra", "caida_proveedor": "fibra",
    "wifi": "wifi", "wireless": "wifi", "wi-fi": "wifi",
    "cableado": "cableado", "cabling": "cableado", "cable": "cableado",
    "cobre": "cobre_dsl", "cobre_dsl": "cobre_dsl", "dsl": "cobre_dsl",
    "hardware": "equipos", "equipos": "equipos", "equipment": "equipos", "modem": "equipos", "router": "equipos",
}  # fmt: skip


def specialty_for(category: str | None, override: str | None = None) -> str | None:
    if override:
        return override
    return CATEGORY_SPECIALTY.get((category or "").strip().lower()) if category else None


@dataclass(frozen=True)
class TicketInfo:
    priority: str = "medium"
    category: str | None = None
    zone: str | None = None
    service_mode: str | None = None


@dataclass(frozen=True)
class Candidate:
    id: UUID
    name: str
    active: bool = True
    availability: str = "disponible"
    schedule: dict = field(default_factory=dict)
    specialties: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()
    level: str = "n1"
    max_open_tickets: int = 5
    open_tickets: int = 0
    last_assigned_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    technician_id: UUID | None
    technician_name: str | None
    reason: str

    @property
    def queued(self) -> bool:
        return self.technician_id is None


def _day_index(token: str) -> int | None:
    return _DAYS.index(token) if token in _DAYS else None


def _days_of(key: str) -> set[int]:
    if "-" in key:
        a, b = (_day_index(p.strip()) for p in key.split("-", 1))
        if a is None or b is None:
            return set()
        return {(a + i) % 7 for i in range(((b - a) % 7) + 1)}
    d = _day_index(key.strip())
    return set() if d is None else {d}


def in_schedule(schedule: dict, now: datetime) -> bool:
    """schedule: {"lun-vie": ["08:00","17:00"], "sab": [...]} evaluated in America/Bogota. Empty = always."""
    if not schedule:
        return True
    local = now.astimezone(BOGOTA)
    for key, span in schedule.items():
        if local.weekday() in _days_of(str(key).lower()) and isinstance(span, list) and len(span) == 2:
            try:
                start, end = (time.fromisoformat(s) for s in span)
            except ValueError:
                continue
            if start <= local.time().replace(tzinfo=None) < end:
                return True
    return False


def _eligible(c: Candidate, t: TicketInfo, specialty: str | None, now: datetime) -> bool:
    return (
        c.active
        and c.availability == "disponible"
        and in_schedule(c.schedule, now)
        and (specialty is None or specialty in c.specialties)
        and (not t.zone or not c.zones or t.zone in c.zones)
        and (t.service_mode != "visita" or c.level == "n2")
        and c.open_tickets < c.max_open_tickets
    )


_EPOCH = datetime.min.replace(tzinfo=BOGOTA)


def _last(c: Candidate) -> datetime:
    return c.last_assigned_at or _EPOCH


def decide(
    ticket: TicketInfo,
    technicians: list[Candidate],
    strategy: str,
    now: datetime,
    *,
    specialty_override: str | None = None,
    continuity_id: UUID | None = None,
    preferred_id: UUID | None = None,
) -> Decision:
    """Pick a technician or return a queued Decision (technician_id None) with an honest reason."""
    if strategy == "manual":
        return Decision(None, None, "estrategia manual: queda en cola para asignación humana")
    specialty = specialty_for(ticket.category, specialty_override)
    pool = [c for c in technicians if _eligible(c, ticket, specialty, now)]
    where = "".join(
        [
            f"especialidad {specialty}" if specialty else "cualquier especialidad",
            f", zona {ticket.zone}" if ticket.zone else "",
            ", visita (n2)" if ticket.service_mode == "visita" else "",
        ]
    )
    if not pool:
        return Decision(None, None, f"sin técnicos disponibles ({where})")
    if continuity_id and (hit := next((c for c in pool if c.id == continuity_id), None)):
        return Decision(hit.id, hit.name, f"{where}, continuidad con caso anterior")
    if preferred_id and (hit := next((c for c in pool if c.id == preferred_id), None)):
        return Decision(hit.id, hit.name, f"{where}, elegido manualmente")
    notes = []
    if ticket.priority in ("high", "critical"):
        n2 = [c for c in pool if c.level == "n2"]
        if n2:
            pool = n2
        notes.append("prioridad alta: n2 con más capacidad libre")
        pool.sort(key=lambda c: (-(c.max_open_tickets - c.open_tickets), _last(c), c.name))
    elif strategy == "round_robin":
        pool.sort(key=lambda c: (_last(c), c.name))
        notes.append("rotación")
    else:
        pool.sort(key=lambda c: (c.open_tickets, _last(c), c.name))
        notes.append("menor carga")
    best = pool[0]
    return Decision(best.id, best.name, f"{where}, {', '.join(notes)}")


def sla_due(created_at: datetime, priority: str, sla_hours: dict) -> datetime | None:
    hours = sla_hours.get(priority)
    return created_at + timedelta(hours=float(hours)) if isinstance(hours, int | float) and hours > 0 else None
