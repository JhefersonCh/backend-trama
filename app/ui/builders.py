# ruff: noqa: E501
"""Pure builders: agent state (facts, hypotheses, trace, ticket) -> validated `ui_component` envelopes.
No I/O, no LLM. Every component carries `spoken` (TTS) and `fallback_text` (unknown component / accessibility)."""

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.agent.explain import render_fact, render_when

from .models import COMPONENTS

# ------------------------------------------------------------------ envelope


def new_id() -> str:
    return f"cmp_{uuid4().hex[:8]}"


def stable_id(*parts: Any) -> str:
    """Same inputs -> same id, so re-emitting updates the component in place (contract §1)."""
    return "cmp_" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:8]


def ui_component(
    component: str, props: dict[str, Any], spoken: str, fallback_text: str, id: str | None = None
) -> dict[str, Any]:
    """Validated S->C frame (without turn_id: the emitter adds it). Raises ValueError on an unknown component or bad props."""
    model = COMPONENTS.get(component)
    if model is None:
        raise ValueError(f"unknown component: {component}")
    clean = model.model_validate(props).model_dump(mode="json", by_alias=True)
    return {
        "type": "ui_component",
        "id": id or new_id(),
        "component": component,
        "props": clean,
        "spoken": spoken.strip(),
        "fallback_text": fallback_text.strip(),
    }


# ------------------------------------------------------------------ shared vocabulary


def confidence_label(cf: float) -> str:
    return "muy probable" if cf >= 0.8 else "probable" if cf >= 0.5 else "posible" if cf >= 0.2 else "poco probable"


STATE_LABELS = {
    "nuevo": "Caso nuevo",
    "entendiendo": "Entendiendo el problema",
    "diagnosticando": "Buscando la causa",
    "intentando_solucion": "Probando una solución",
    "verificando": "Comprobando si ya funciona",
    "resuelto": "Resuelto",
    "escalado": "En manos de un técnico",
    "seguimiento": "En seguimiento",
    "cerrado": "Cerrado",
}
STATE_NEXT = {
    "nuevo": "Cuénteme qué pasa con su conexión",
    "entendiendo": "Responder unas preguntas cortas",
    "diagnosticando": "Confirmar la causa con una o dos preguntas más",
    "intentando_solucion": "Seguir los pasos indicados",
    "verificando": "Confirmar si ya tiene internet",
    "resuelto": "Nada más: le escribiré para confirmar que todo siga bien",
    "escalado": "El técnico revisará su caso según lo acordado",
    "seguimiento": "Confirmar si el arreglo del técnico funcionó",
    "cerrado": "Si vuelve a fallar, escríbame",
}
TICKET_STATUS_LABELS = {
    "open": "Abierto",
    "assigned": "Asignado a un técnico",
    "in_progress": "En atención",
    "resolved": "Resuelto por el técnico",
    "closed": "Cerrado",
}
SOURCE_LABELS = {
    "user_reported": "lo dijo usted",
    "browser_measured": "medido en el navegador",
    "local_agent": "medido en el equipo",
    "router": "leído del router",
    "public_api": "consulta pública",
    "photo": "visto en la foto",
    "technician": "lo registró el técnico",
    "system": "sistema",
}
ENUM_LABELS: dict[str, dict[str, str]] = {
    "luz_modem": {"verde": "Verde", "roja": "Roja", "naranja": "Naranja", "apagada": "Apagada", "parpadea": "Parpadea"},
    "luz_tras_reinicio": {"normal": "Quedó normal", "roja": "Sigue con falla"},
    "modalidad": {"visita": "Visita a domicilio", "remoto": "Remota (por teléfono)"},
    "franja": {"manana": "Mañana", "tarde": "Tarde", "noche": "Noche", "cualquiera": "Cualquier horario"},
}
UNKNOWN_LABEL = "No sé"
YES_NO = [("Sí", True), ("No", False)]


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _get(o: Any, k: str, default: Any = None) -> Any:
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def _iso(v: Any) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v is not None else ""


def _join_or(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " o " + items[-1] if items else ""


# ------------------------------------------------------------------ quick_replies


def options_for_fact(fact: str, spec: dict[str, Any], ui: dict[str, Any] | None = None) -> list[dict[str, Any]] | None:
    """Options from the fact's closed domain (bool -> Sí/No/No sé; enum -> labelled values + No sé). None = free text only."""
    ui = ui or {}
    if isinstance(ui.get("options"), list) and ui["options"]:
        return [{"label": str(o["label"]), "value": o.get("value")} for o in ui["options"] if isinstance(o, dict)]
    t = spec.get("type")
    if t == "bool":
        return [{"label": lbl, "value": v} for lbl, v in YES_NO] + [{"label": UNKNOWN_LABEL, "value": None}]
    if t == "enum":
        labels = {**ENUM_LABELS.get(fact, {}), **(ui.get("labels") or {})}
        values = [v for v in spec.get("values", []) if v != "desconocida"]
        opts = [{"label": labels.get(v, _cap(v.replace("_", " "))), "value": v} for v in values]
        unknown = "desconocida" if "desconocida" in spec.get("values", []) else None
        return opts + [{"label": UNKNOWN_LABEL, "value": unknown}]
    return None


def quick_replies_for(
    action: dict[str, Any], fact_catalog: dict[str, dict], question: str | None = None, id: str | None = None
) -> dict[str, Any] | None:
    """`action` is the YAML spec of an `ask` action (uses `fact` and optional `ui.question/labels/options`)."""
    fact = action.get("fact")
    spec = fact_catalog.get(fact or "")
    if not fact or spec is None:
        return None
    ui = action.get("ui") if isinstance(action.get("ui"), dict) else {}
    if ui.get("component", "quick_replies") != "quick_replies":
        return None
    opts = options_for_fact(fact, spec, ui)
    if not opts:
        return None
    q = ui.get("question") or question or f"¿{_cap(fact.replace('_', ' '))}?"
    labels = [o["label"] for o in opts]
    spoken = "" if spec.get("type") == "bool" else f"Puede responder: {_join_or([x.lower() for x in labels])}."
    return ui_component(
        "quick_replies",
        {"fact": fact, "question": q, "options": opts, "allow_free_text": True},
        spoken,
        f"{q} Opciones: {' / '.join(labels)}.",
        id,
    )


def yes_no_replies(
    pseudo_fact: str, question: str, yes: str = "Sí", no: str = "No", id: str | None = None
) -> dict[str, Any]:
    """Chips for a question that is not a catalog fact (verification, offer to escalate). The backend maps the
    value back to plain text, so the normal text pipeline interprets it."""
    return ui_component(
        "quick_replies",
        {"fact": pseudo_fact, "question": question, "options": [{"label": yes, "value": True}, {"label": no, "value": False}],
         "allow_free_text": True},
        "",
        f"{question} Opciones: {yes} / {no}.",
        id,
    )  # fmt: skip


# ------------------------------------------------------------------ checklist


def checklist_steps(action: dict[str, Any]) -> list[dict[str, str]]:
    ui = action.get("ui") if isinstance(action.get("ui"), dict) else {}
    out = []
    for i, s in enumerate(ui.get("steps") or [], 1):
        if isinstance(s, str) and s.strip():
            out.append({"id": f"p{i}", "text": s.strip()})
        elif isinstance(s, dict) and s.get("text"):
            out.append({"id": str(s.get("id") or f"p{i}"), "text": str(s["text"]).strip()})
    return out


def checklist_props(
    action_id: str, action: dict[str, Any], statuses: dict[str, str] | None = None
) -> dict[str, Any] | None:
    steps = checklist_steps(action)
    if not steps:
        return None
    ui = action.get("ui") or {}
    statuses = statuses or {}
    steps = [{**s, "status": statuses.get(s["id"], "pending")} for s in steps]
    current = next((s["id"] for s in steps if s["status"] != "done"), None)
    title = ui.get("title") or _cap(action_id.removeprefix("guide_").replace("_", " "))
    return {"action_id": action_id, "title": title, "steps": steps, "current": current}


def checklist_envelope(props: dict[str, Any], id: str | None = None) -> dict[str, Any]:
    steps = props["steps"]
    n = len(steps)
    cur = next((i for i, s in enumerate(steps) if s["id"] == props.get("current")), None)
    if cur is None:
        spoken = "Listo, completó todos los pasos."
    elif steps[cur]["status"] == "failed":
        spoken = f"El paso {cur + 1} no se pudo completar: {steps[cur]['text']}"
    else:
        spoken = f"Paso {cur + 1} de {n}: {steps[cur]['text']}"
    mark = {"done": " (hecho)", "failed": " (no se pudo)", "pending": ""}
    fallback = f"{props['title']}: " + " ".join(f"{i}) {s['text']}{mark[s['status']]}" for i, s in enumerate(steps, 1))
    return ui_component("checklist", props, spoken, fallback, id)


def checklist_for(
    action_id: str, action: dict[str, Any], statuses: dict[str, str] | None = None, id: str | None = None
) -> dict[str, Any] | None:
    """Checklist from the action's `ui.steps` (YAML). None when the action declares no steps."""
    props = checklist_props(action_id, action, statuses)
    return checklist_envelope(props, id) if props else None


def update_checklist(props: dict[str, Any], step_id: str, status: str) -> dict[str, Any]:
    """Pure: new props with one step marked done/failed; `current` moves to the first step not done."""
    steps = [{**s, "status": status if s["id"] == step_id else s["status"]} for s in props["steps"]]
    current = next((s["id"] for s in steps if s["status"] != "done"), None)
    return {**props, "steps": steps, "current": current}


def checklist_done(props: dict[str, Any]) -> bool:
    return all(s["status"] == "done" for s in props["steps"])


# ------------------------------------------------------------------ form (K3)

MODE_OPTIONS = [
    {"label": "Visita a domicilio", "value": "visita"},
    {"label": "Remota (por teléfono)", "value": "remoto"},
]
SLOT_OPTIONS = [
    {"label": "Mañana", "value": "manana"},
    {"label": "Tarde", "value": "tarde"},
    {"label": "Noche", "value": "noche"},
    {"label": "Cualquier horario", "value": "cualquiera"},
]


def _default_zone_label(z: str) -> str:
    return z.replace("_", " ").title()


def assignment_form(
    zones: list[str],
    known: dict[str, Any],
    contact_known: bool = False,
    zone_label: Callable[[str], str] = _default_zone_label,
    id: str | None = None,
) -> dict[str, Any] | None:
    """Fields only for what is still missing (zona/modalidad/franja) + optional contact with consent."""
    fields: list[dict[str, Any]] = []
    if not known.get("zona"):
        fields.append({"name": "zona", "label": "Municipio", "type": "select",
                       "options": [{"label": zone_label(z), "value": z} for z in zones], "placeholder": "Elija su municipio"})  # fmt: skip
    if not known.get("modalidad"):
        fields.append(
            {"name": "modalidad", "label": "¿Cómo prefiere la atención?", "type": "select", "options": MODE_OPTIONS}
        )
    if not known.get("franja"):
        fields.append({"name": "franja", "label": "Horario preferido", "type": "select", "options": SLOT_OPTIONS})
    if not contact_known:
        fields.append({"name": "contacto", "label": "Autorizo que el técnico me contacte a este teléfono o correo (opcional)",
                       "type": "consent_text", "placeholder": "Teléfono o correo"})  # fmt: skip
    if not fields:
        return None
    asks = [{"zona": "su municipio", "modalidad": "si prefiere visita o atención remota", "franja": "el horario",
             "contacto": "un contacto opcional"}[f["name"]] for f in fields]  # fmt: skip
    return ui_component(
        "form",
        {"form_id": "assignment", "title": "Datos para el técnico", "fields": fields, "submit_label": "Enviar datos"},
        "Puede llenar el formulario en pantalla o decirme los datos.",
        "Para asignar el técnico necesito " + ", ".join(asks[:-1]) + (" y " if len(asks) > 1 else "") + asks[-1] + ".",
        id,
    )


# ------------------------------------------------------------------ case_card / hypothesis_bars


def _labelled(hyps: list[dict[str, Any]], labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    labels = labels or {}
    return [{**h, "label": h.get("label") or labels.get(h["key"], h["key"])} for h in hyps]


def top_hypothesis(hyps: list[dict[str, Any]]) -> dict[str, Any] | None:
    pos = [h for h in hyps if h.get("cf", 0) > 0]
    if not pos:
        return None
    return sorted(pos, key=lambda h: (h.get("status") != "aceptada", -h["cf"]))[0]


def _ticket_props(ticket: Any) -> dict[str, Any] | None:
    if not ticket:
        return None
    number = _get(ticket, "ticket_number") or _get(ticket, "number")
    if not number:
        return None
    status = _get(ticket, "status") or "open"
    tech = _get(ticket, "technician_name")
    if tech is None and (t := _get(ticket, "technician")) is not None:
        tech = _get(t, "name") if not isinstance(t, str) else t
    sla = _get(ticket, "sla_due_at")
    return {"number": str(number), "status": str(status), "status_label": TICKET_STATUS_LABELS.get(status, status),
            "technician": tech, "sla_due_at": _iso(sla) if sla else None}  # fmt: skip


def case_card(
    case_view: dict[str, Any], ticket: Any = None, next_step: str | None = None, id: str | None = None
) -> dict[str, Any]:
    state = case_view.get("state") or "nuevo"
    top = top_hypothesis(_labelled(case_view.get("hypotheses") or []))
    tp = _ticket_props(ticket if ticket is not None else case_view.get("ticket"))
    nxt = next_step or STATE_NEXT.get(state)
    props = {
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "top_hypothesis": {
            "key": top["key"],
            "label": top["label"],
            "cf": round(float(top["cf"]), 3),
            "confidence_label": confidence_label(top["cf"]),
        }
        if top
        else None,  # fmt: skip
        "next_step": nxt,
        "ticket": tp,
    }
    said = [f"{props['state_label']}."]
    if top:
        said.append(f"Lo más {('probable' if top['cf'] < 0.8 else 'seguro')}: {top['label'].lower()}.")
    if tp:
        said.append(f"Su caso es el {tp['number']}" + (f", con {tp['technician']}." if tp["technician"] else "."))
    fb = list(said) + ([f"Próximo paso: {nxt}."] if nxt else [])
    return ui_component("case_card", props, " ".join(said), " ".join(fb), id)


def hypothesis_bars(
    hypotheses: list[dict[str, Any]],
    thresholds: dict[str, float],
    labels: dict[str, str] | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    items = sorted(_labelled(hypotheses, labels), key=lambda h: (-h["cf"], h["key"]))[:3]
    bars = [{"key": h["key"], "label": h["label"], "cf": round(float(h["cf"]), 3), "status": h.get("status", "viva"),
             "confidence_label": confidence_label(h["cf"])} for h in items]  # fmt: skip
    if bars and bars[0]["cf"] > 0:
        spoken = f"Lo más probable es {bars[0]['label'].lower()}: {bars[0]['confidence_label']}."
    else:
        spoken = "Todavía no tengo una causa clara."
    fb = "Posibles causas: " + "; ".join(f"{b['label']}: {b['confidence_label']}" for b in bars) if bars else spoken
    return ui_component(
        "hypothesis_bars",
        {
            "items": bars,
            "thresholds": {
                "accept": float(thresholds.get("accept", 0.8)),
                "reject": float(thresholds.get("reject", -0.5)),
            },
        },
        spoken,
        fb,
        id,
    )


# ------------------------------------------------------------------ timeline

_EVENT_TITLES = {
    "created": "Caso registrado",
    "assigned": "Técnico asignado",
    "reassigned": "Técnico reasignado",
    "unassigned_queue": "En cola: esperando técnico disponible",
    "status_changed": "Cambio de estado",
    "contact_registered": "Contacto registrado",
}
_GUIDE_TITLE = "Paso guiado"


def timeline(
    case_view: dict[str, Any],
    ticket_timeline: Any = None,
    guide_titles: dict[str, str] | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    guide_titles = guide_titles or {}
    items: list[dict[str, Any]] = []
    for f in case_view.get("facts") or []:
        if f.get("key") in ("zona", "modalidad", "franja"):
            continue
        text = f.get("text") or render_fact(f["key"], f.get("value"))
        items.append({"at": _iso(f.get("created_at")), "kind": "fact", "title": _cap(text),
                      "detail": _cap(SOURCE_LABELS.get(f.get("source") or "", f.get("source") or "")) or None})  # fmt: skip
    for t in case_view.get("trace") or []:
        a, ph = t.get("action") or "", t.get("phase")
        if ph == "actuar" and a.startswith("guide_"):
            items.append({"at": _iso(t.get("created_at")), "kind": "step",
                          "title": f"{_GUIDE_TITLE}: {guide_titles.get(a, a.removeprefix('guide_').replace('_', ' '))}", "detail": None})  # fmt: skip
        elif ph == "verificar" and a == "verified_working":
            items.append(
                {
                    "at": _iso(t.get("created_at")),
                    "kind": "system",
                    "title": "Usted confirmó que ya funciona",
                    "detail": None,
                }
            )
        elif ph == "verificar" and a == "still_failing":
            items.append(
                {
                    "at": _iso(t.get("created_at")),
                    "kind": "system",
                    "title": "Seguía fallando después del paso",
                    "detail": None,
                }
            )
        elif ph == "proactivo":
            items.append(
                {"at": _iso(t.get("created_at")), "kind": "followup", "title": "Seguimiento", "detail": t.get("reason")}
            )
    if ticket_timeline is not None:
        number = _get(ticket_timeline, "ticket_number")
        for e in _get(ticket_timeline, "events") or []:
            et = _get(e, "event_type") or ""
            new = _get(e, "new_value")
            if et == "technician_note":
                who = (new or {}).get("technician_name") if isinstance(new, dict) else None
                items.append({"at": _iso(_get(e, "created_at")), "kind": "note", "title": f"Nota de {who or 'el técnico'}",
                              "detail": (new or {}).get("text") if isinstance(new, dict) else _get(e, "details")})  # fmt: skip
                continue
            title = _EVENT_TITLES.get(et, _cap(et.replace("_", " ")))
            if et == "created" and number:
                title = f"Caso {number} registrado"
            detail = _get(e, "details")
            if et == "status_changed" and isinstance(new, dict | str):
                st = new.get("status") if isinstance(new, dict) else new
                detail = TICKET_STATUS_LABELS.get(st, st)
            items.append({"at": _iso(_get(e, "created_at")), "kind": "ticket", "title": title,
                          "detail": detail if detail and detail != "ticket created" else None})  # fmt: skip
    items = sorted((i for i in items if i["at"]), key=lambda i: i["at"])[-25:]
    spoken = (
        f"Su caso tiene {len(items)} novedades; la última: {items[-1]['title'].lower()}."
        if items
        else "Todavía no hay novedades."
    )
    fb = " · ".join(f"{i['title']}" + (f" ({i['detail']})" if i["detail"] else "") for i in items[-8:]) or spoken
    return ui_component("timeline", {"items": items}, spoken, fb, id)


# ------------------------------------------------------------------ topology

NETWORK_FACTS = (
    "luz_modem", "luz_tras_reinicio", "otros_equipos_navegan", "ip_apipa", "navega_por_ip", "resuelve_nombres",
    "senal_wifi_pct", "funciona_por_cable", "error_contrasena", "caida_reportada_zona",
)  # fmt: skip
_NODES = (
    ("isp", "isp", "Proveedor de internet"),
    ("modem", "modem", "Módem"),
    ("router", "router", "Router / Wi-Fi"),
    ("devices", "device", "Otros equipos"),
    ("pc", "pc", "Este equipo"),
)
_EDGES = (("isp", "modem"), ("modem", "router"), ("router", "devices"), ("router", "pc"))
_SEVERITY = {"unknown": 0, "ok": 1, "warn": 2, "fail": 3}


def topology_from_facts(facts: dict[str, Any], id: str | None = None) -> dict[str, Any]:
    """Home network isp -> modem -> router -> {devices, pc}; each node takes the most severe status its facts imply."""
    st = {n: "unknown" for n, _, _ in _NODES}
    det: dict[str, str | None] = {n: None for n, _, _ in _NODES}

    def put(node: str, status: str, detail: str) -> None:
        if _SEVERITY[status] > _SEVERITY[st[node]]:
            st[node], det[node] = status, detail
        elif _SEVERITY[status] == _SEVERITY[st[node]] and det[node] is None:
            det[node] = detail

    luz, tras = facts.get("luz_modem"), facts.get("luz_tras_reinicio")
    otros, apipa = facts.get("otros_equipos_navegan"), facts.get("ip_apipa")
    by_ip, dns = facts.get("navega_por_ip"), facts.get("resuelve_nombres")
    wifi, cable, pwd = facts.get("senal_wifi_pct"), facts.get("funciona_por_cable"), facts.get("error_contrasena")
    if luz in ("roja", "naranja"):
        put("modem", "fail", f"La luz de internet está {luz}: no recibe señal")
    elif luz == "apagada":
        put("modem", "fail", "No muestra luces: puede estar apagado o sin corriente")
    elif luz == "parpadea":
        put("modem", "warn", "La luz de internet parpadea: está intentando conectarse")
    elif luz == "verde":
        put("modem", "ok", "Luz de internet en verde")
        put("isp", "ok", "La señal llega al módem")
    if tras == "roja":
        put("modem", "fail", "Sigue en rojo después de reiniciarlo")
        put("isp", "fail", "No llega señal del proveedor")
    elif tras == "normal":
        put("modem", "ok", "Quedó normal después de reiniciarlo")
        put("isp", "ok", "La señal volvió a llegar")
    if facts.get("caida_reportada_zona") is True:
        put("isp", "fail", "Hay una caída reportada en su zona")
    if otros is False:
        put("devices", "fail", "Ningún otro equipo tiene internet")
        put("pc", "fail", "Sin internet")
    elif otros is True:
        put("devices", "ok", "Los otros equipos sí navegan")
        put("router", "ok", "Da internet a los demás equipos")
        put("modem", "ok", "Funciona para los demás equipos")
        put("isp", "ok", "Hay servicio")
        if apipa is not True:
            put("pc", "warn", "Solo este equipo falla")
    if apipa is True:
        put("pc", "fail", "Tiene una dirección 169.254.x.x: no recibió dirección del router")
    if by_ip is True and dns is False:
        put("pc", "warn", "Abre páginas por IP pero no traduce nombres (DNS)")
        put("isp", "ok", "Hay salida a internet")
    elif by_ip is False:
        put("pc", "fail", "No navega ni siquiera por dirección IP")
    if isinstance(wifi, int | float) and not isinstance(wifi, bool) and wifi < 50:
        put("pc", "warn", f"Señal Wi-Fi débil ({wifi} %)")
    if cable is True:
        put("router", "warn", "El Wi-Fi falla, pero por cable funciona")
    if pwd is True:
        put("pc", "warn", "La red rechaza la clave del Wi-Fi")
    if st["modem"] == "fail" and st["isp"] == "unknown" and luz in ("roja", "naranja"):
        det["isp"] = "Sin confirmar si la falla es del proveedor"

    phone = facts.get("equipo_afectado") == "celular"
    nodes = [
        {
            "id": n,
            "kind": "device" if (phone and n == "pc") else k,
            "label": "Su celular" if (phone and n == "pc") else lbl,
            "status": st[n],
            "detail": det[n],
        }
        for n, k, lbl in _NODES
    ]

    def edge(a: str, b: str) -> str:
        if "fail" in (st[a], st[b]):
            return "fail"
        return "ok" if st[a] == "ok" and st[b] in ("ok", "warn") else "unknown"

    edges = [{"from": a, "to": b, "status": edge(a, b)} for a, b in _EDGES]
    bad = [n for n in nodes if n["status"] in ("fail", "warn") and n["detail"]]
    good = [n["label"].lower() for n in nodes if n["status"] == "ok"]
    parts = [f"{n['label']}: {n['detail'][:1].lower() + n['detail'][1:]}" for n in bad[:3]]
    if good:
        parts.append(("Están bien: " if len(good) > 1 else "Está bien: ") + ", ".join(good))
    spoken = ". ".join(parts) + "." if parts else "Todavía no sé en qué parte de la red está la falla."
    word = {"ok": "bien", "fail": "falla", "warn": "con problemas", "unknown": "sin revisar"}
    fb = "Su red: " + "; ".join(
        f"{n['label']} ({word[n['status']]})" + (f": {n['detail']}" if n["detail"] else "") for n in nodes
    )
    return ui_component("topology", {"nodes": nodes, "edges": edges}, spoken, fb, id)


def topology_signature(component: dict[str, Any]) -> str:
    return ",".join(f"{n['id']}={n['status']}" for n in component["props"]["nodes"])


def known_network_facts(facts: dict[str, Any]) -> int:
    return sum(1 for k in NETWORK_FACTS if facts.get(k) is not None)


# ------------------------------------------------------------------ diagnosis_flow

_ACTION_LABELS = {
    "offer_escalation": "Ofrecer la revisión de un técnico",
    "escalate_ticket": "Escalar a un técnico",
    "ticket:create": "Caso registrado con un técnico",
    "verified_working": "Comprobado: ya funciona",
}


def _action_label(action: str, actions: dict[str, dict]) -> str | None:
    if action in _ACTION_LABELS:
        return _ACTION_LABELS[action]
    if action.startswith("explain:"):
        return "Explicar la causa"
    spec = actions.get(action) or {}
    if action.startswith("guide_") or spec.get("kind") == "guided_step":
        title = (spec.get("ui") or {}).get("title") or action.removeprefix("guide_").replace("_", " ")
        return f"Paso guiado: {title}"
    return None


def diagnosis_flow_from_trace(
    trace: list[dict[str, Any]],
    ruleset: Any,
    facts: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
    questions: dict[str, str] | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    """questions asked -> answers (facts) -> rules fired (plain Spanish) -> accepted/top hypothesis -> action.
    `trace` oldest first (decision_trace rows); `facts` the active facts; `hypotheses` the current ones."""
    questions = questions or {}
    actions = ruleset.actions or {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    ids: set[str] = set()

    def node(nid: str, kind: str, label: str, detail: str | None = None) -> None:
        if nid not in ids:
            ids.add(nid)
            nodes.append({"id": nid, "kind": kind, "label": label, "detail": detail})

    asked: dict[str, str] = {}  # fact -> question node id
    for t in trace:
        a = t.get("action") or ""
        spec = actions.get(a) or {}
        if t.get("phase") == "actuar" and spec.get("kind") == "ask" and spec.get("fact"):
            q = (spec.get("ui") or {}).get("question") or questions.get(a) or _cap(spec["fact"].replace("_", " ")) + "?"
            node(f"q_{a}", "question", q)
            asked[spec["fact"]] = f"q_{a}"
    known = {f["key"]: f for f in facts if f["key"] not in ("zona", "modalidad", "franja")}
    for k, f in known.items():
        node(
            f"a_{k}",
            "answer",
            _cap(render_fact(k, f.get("value"))),
            _cap(SOURCE_LABELS.get(f.get("source") or "", "")) or None,
        )
        if k in asked:
            edges.append({"from": asked[k], "to": f"a_{k}", "label": None})
    labels = {k: v.get("label", k) for k, v in ruleset.hypotheses.items()}
    target = top_hypothesis(_labelled(hypotheses, labels))
    fired = next((list(t.get("rules_fired") or []) for t in reversed(trace) if t.get("phase") == "creer"), [])
    by_id = {r.id: r for r in ruleset.rules}
    for rid in fired:
        r = by_id.get(rid)
        if r is None or (target and target["key"] not in r.then):
            continue
        eff = "; ".join(f"{'+' if cf > 0 else '−'}{abs(cf):.1f} {labels.get(h, h).lower()}" for h, cf in r.then.items())
        node(f"r_{rid}", "rule", f"Regla {rid}: si {render_when(r.when)}", eff)
        for k in r.when:
            if f"a_{k}" in ids:
                edges.append({"from": f"a_{k}", "to": f"r_{rid}", "label": None})
    act_label = None
    if target:
        hid = f"h_{target['key']}"
        node(hid, "hypothesis", target["label"], f"{confidence_label(target['cf'])} (certeza {target['cf']:.2f})")
        for rid in fired:
            r = by_id.get(rid)
            if r and target["key"] in r.then and f"r_{rid}" in ids:
                edges.append({"from": f"r_{rid}", "to": hid, "label": f"{r.then[target['key']]:+.1f}"})
        for t in reversed(trace):
            if t.get("phase") in ("decidir", "actuar", "verificar") and (
                lbl := _action_label(t.get("action") or "", actions)
            ):
                act_label = lbl
                break
        if act_label:
            node("act", "action", act_label)
            edges.append({"from": hid, "to": "act", "label": None})
    q_n = sum(1 for n in nodes if n["kind"] == "question")
    r_ids = [n["id"][2:] for n in nodes if n["kind"] == "rule"]
    if target:
        spoken = (
            (f"Le hice {q_n} pregunta{'s' if q_n != 1 else ''}; " if q_n else "")
            + (f"con sus respuestas se cumplieron las reglas {', '.join(r_ids)}, " if r_ids else "")
            + f"y por eso lo más probable es {target['label'].lower()}."
        )
    else:
        spoken = "Todavía no llego a una conclusión: sigo descartando causas con preguntas cortas."
    fb = " → ".join(n["label"] for n in nodes) or spoken
    return ui_component("diagnosis_flow", {"nodes": nodes, "edges": edges}, _cap(spoken), fb, id)


# ------------------------------------------------------------------ compare_cards / report_card / image_annotated


def compare_cards(question: str, options: list[dict[str, Any]], id: str | None = None) -> dict[str, Any]:
    titles = [o["title"] for o in options]
    return ui_component(
        "compare_cards",
        {"question": question, "options": options},
        f"{question} Puede elegir: {_join_or([t.lower() for t in titles])}.",
        f"{question} " + " | ".join(f"{o['title']}: {o['summary']}" for o in options),
        id,
    )


def report_card(session_id: str, kind: str = "informe", id: str | None = None) -> dict[str, Any]:
    title = "Informe del caso" if kind == "informe" else "Guía paso a paso"
    base = f"/api/cases/{session_id}"
    html = f"{base}/report.html?kind={kind}"
    return ui_component(
        "report_card",
        {"title": title, "html_url": html, "pdf_url": f"{base}/report.pdf?kind={kind}", "kind": kind},
        f"Le dejo {'el informe de su caso' if kind == 'informe' else 'la guía'} para descargar o imprimir.",
        f"{title}: {html}",
        id,
    )


_LED_LABELS = {
    "internet": "Internet",
    "los": "LOS",
    "pon": "PON",
    "power": "Encendido",
    "wifi": "Wi-Fi",
    "lan": "LAN",
    "otro": "Otra luz",
}
_LED_COLORS = ("roja", "verde", "naranja", "apagada")
_COLOR_PHRASE = {
    "roja": "en rojo",
    "verde": "en verde",
    "naranja": "en naranja",
    "apagada": "apagada",
    "parpadea": "parpadeando",
}
_SIGNAL_LEDS = ("internet", "los", "pon")  # the leds that tell whether the provider's signal arrives


def facts_from_analysis(analysis: dict[str, Any] | None) -> tuple[list[tuple[str, Any]], dict[str, Any] | None]:
    """(catalog facts, the led they came from). LOS/PON lit red mean the same as a red internet light."""
    leds = [x for x in (analysis or {}).get("leds") or [] if isinstance(x, dict)]
    led = next(
        (x for lbl in _SIGNAL_LEDS for x in leds if x.get("label") == lbl and x.get("color") in _LED_COLORS), None
    )
    if led is None:
        return [], None
    color = led["color"]
    if led.get("label") in ("los",) and color == "verde":
        return [], led  # a green LOS is unusual; do not guess
    value = "parpadea" if led.get("blink") and color == "verde" else color
    return [("luz_modem", value)], led


def image_annotated(
    media: dict[str, Any], url: str, analysis: dict[str, Any] | None, id: str | None = None
) -> dict[str, Any]:
    leds = [x for x in (analysis or {}).get("leds") or [] if isinstance(x, dict)]
    boxes = []
    for x in leds:
        bb = x.get("bbox")
        if isinstance(bb, list) and len(bb) == 4 and all(isinstance(v, int | float) for v in bb):
            boxes.append({"bbox": [max(0.0, min(1.0, float(v))) for v in bb], "label": _LED_LABELS.get(x.get("label"), "Luz"),
                          "color": x.get("color") if x.get("color") in _LED_COLORS else "desconocido"})  # fmt: skip
    found, led = facts_from_analysis(analysis)
    question, confirm = None, None
    if found and led is not None:
        k, v = found[0]
        question = f"¿Es esta la luz de {_LED_LABELS.get(led.get('label'), 'internet')} {_COLOR_PHRASE.get(v, v)}?"
        confirm = {"key": k, "value": v}
    seen = ", ".join(f"{b['label']} {_COLOR_PHRASE.get(b['color'], '')}".strip() for b in boxes) or "no distinguí luces"
    return ui_component(
        "image_annotated",
        {"media_id": str(media.get("id")), "url": url, "width": media.get("width"), "height": media.get("height"),
         "boxes": boxes, "question": question, "confirm_fact": confirm},
        question or "Revisé la foto, pero no distingo bien las luces.",
        f"En la foto veo: {seen}." + (f" {question}" if question else ""),
        id,
    )  # fmt: skip
