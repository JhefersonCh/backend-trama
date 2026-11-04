# ruff: noqa: E501
"""Deterministic turn policy (no LLM): intent router, audience detector, per-turn instruction, answer post-filter.

The system prompt + tools stay byte-stable across turns (llama.cpp prompt cache); everything that varies per turn
(intent format, audience, voice/text, retrieved sources) travels in a trailing message built here."""

import re
import unicodedata

# ------------------------------------------------------------------ intent
SMALLTALK, TICKET, INVENTORY, OUTPUT, GUIDE, DIAGNOSTIC, CLARIFY = (
    "smalltalk",
    "ticket",
    "inventory",
    "output",
    "guide",
    "diagnostic",
    "clarify",
)

_SMALLTALK = re.compile(
    r"^\W*(hola+|buen[oa]s?( (d[ií]as|tardes|noches))?|hey|qu[eé] tal|gracias|muchas gracias|ok(ey)?|vale|listo|perfecto|genial|"
    r"de nada|chao|adi[oó]s|hasta luego|buenas)\W*$",
    re.I,
)
_TICKET = re.compile(
    r"\bTRM-\d+|\bticket|\b(crear|abrir|abre|crea|registrar|registra)\b.*\b(caso|reporte|solicitud)\b|\bestado de (mi|el|un) (caso|reporte)",
    re.I,
)
_INVENTORY = re.compile(
    r"\b(sede|inventario|vlan|equipos? registrados?|dispositivos? registrados?|qu[eé] redes)\b", re.I
)
_OUTPUT = re.compile(
    r"ipv4|ping statistics|estad[ií]sticas de ping|\binet \d|^\s*server:|default gateway|puerta de enlace predeterminada|\bttl=",
    re.I | re.M,
)
_GUIDE = re.compile(
    r"\bno s[eé] c[oó]mo\b|\bc[oó]mo (hago|lo hago|se hace|hago eso)\b|\bno s[eé] qu[eé] (es|hacer)\b|\bay[uú]dame con eso\b",
    re.I,
)
_CLARIFY = re.compile(
    r"\bqu[eé] (significa|quiere decir|es eso)\b|\bpor qu[eé]\b|\bexpl[ií]qu?eme\b|\bexpl[ií]came\b|\bno entiendo\b",
    re.I,
)
_REF = re.compile(r"\b(eso|esto|esa|ese|esos|esas|lo de|lo que|luz|luces)\b", re.I)

_TECH = re.compile(
    r"\b(ip|ipv4|ipv6|dhcp|dns|vlan|ping|gateway|ipconfig|ifconfig|nslookup|tracert|traceroute|apipa|ssid|subred|m[aá]scara|"
    r"mtu|nat|tcp|udp|arp|netmask|puerta de enlace|dirección ip|latencia|switch|trunk|ttl|dig|netstat|bssid|wpa2?|wpa3|link-local)\b|\b\d{1,3}(\.\d{1,3}){3}\b",
    re.I,
)

TICKET_CREATE = "ticket_create"
CONSULTA = "consulta"
CLOSING = "closing"
NO_RAG_INTENTS = {SMALLTALK, TICKET, INVENTORY, GUIDE, TICKET_CREATE, CLOSING}

# Thanks / goodbye ("muchas gracias", "no tengo más dudas, hasta luego"): end the case politely, no new diagnosis.
_CLOSING = re.compile(
    r"\b(gracias|hasta luego|hasta pronto|chao|ch[aá]o|adi[oó]s|bendiciones|feliz (d[ií]a|tarde|noche)|que est[eé] bien)\b|"
    r"\bno tengo m[aá]s (dudas|preguntas)\b|\beso (es|ser[ií]a) todo\b|\bnada m[aá]s\b|\bya (me )?(qued[oó]|est[aá]) claro\b|\bme (sirvi[oó]|ayud[oó])\b",
    re.I,
)
# A message that still reports a problem is not a goodbye ("gracias, pero sigue sin internet").
_STILL_PROBLEM = re.compile(
    r"\bpero\b|\bsigue|\btodav[ií]a\b|\ba[uú]n (no|sigue)\b|no (me )?(funciona|sirve|carga|conecta|navega)|"
    r"no (tengo|hay) (internet|wifi|wi-fi|conexi[oó]n|se[nñ]al|red)|\bluz|\bproblema\b|\berror\b|\bfalla",
    re.I,
)

# The user asks for a technician / a person / a ticket (robust to STT noise: "técnico de la previsión").
_CREATE_REQ = re.compile(
    r"\bt[eé]cnic|\bque (lo |la |los |las |me lo |se lo )?revis|\b(que )?(lo |me lo |se lo )?regi?st?r?(a|ar|e|es|en)\b|"
    r"\b(abr\w+|cre\w+|gener\w+|haz\w*|hac\w+|levant\w+)\b[^.?!]{0,20}\b(ticket|caso|reporte|solicitud)\b|"
    r"\bquiero que alguien\b|\balguien (que|de)\b|\b(hablar|comunicar\w*) con (una |un )?(persona|humano|agente|asesor)\b|\buna persona\b",
    re.I,
)
# Acceptance must OPEN the reply ("sí", "bueno sí", "dale"): "es un samsung, y si sale…" / "¿y si compro…?" are not a yes.
_AFFIRM = re.compile(
    r"^\W*(s[ií]|dale|claro|por favor|h[aá]galo|hazlo|me gustar[ií]a|de acuerdo|ok(ey)?|bueno|perfecto|adelante|listo|quiero)\b",
    re.I,
)
_NEGATE = re.compile(r"^\W*no\b|\bno (quiero|gracias|hace falta|es necesario)\b|\beso (era|es) todo\b|\bnada m[aá]s\b|\?", re.I)


_CONSULTA = re.compile(
    r"\bc[oó]mo (configuro|configurar|hago|veo|puedo|se)\b|\bqu[eé] (significa|es(?! lo\b|to\b|a\b)|quiere decir)\b|\bdiferencia (hay )?entre\b|"
    r"\bpara qu[eé] sirve\b|\bexpl[ií]que?me\b|\bcu[aá]l es la diferencia\b",
    re.I,
)
_FAILURE = re.compile(
    r"\bno (funciona|carga|navega|tengo|hay|puedo conectar|me conecta)|\bse me fue\b|\bse cae\b|\bsin internet\b|\bno (me )?(deja|abre)\b",
    re.I,
)


def classify_intent(text: str, prev_assistant: str = "") -> str:
    t = text.strip()
    if (
        prev_assistant
        and offered_ticket(prev_assistant)
        and len(t) <= 120
        and _AFFIRM.search(t)
        and not _NEGATE.search(t)
    ):
        return TICKET_CREATE
    if _CLOSING.search(t) and len(t) <= 160 and not _STILL_PROBLEM.search(t):
        return CLOSING
    if _SMALLTALK.match(t):
        return SMALLTALK
    if _OUTPUT.search(t) or (t.count("\n") >= 4 and len(t) > 120):
        return OUTPUT
    if (
        len(t) <= 250
        and _CREATE_REQ.search(t)
        and not _NEGATE.search(t)
        and not re.search(r"\bTRM-\d+|\bestado\b", t, re.I)
    ):
        return TICKET_CREATE
    if _TICKET.search(t):
        return TICKET
    if _INVENTORY.search(t) and not _TECH_STRONG.search(t):
        return INVENTORY
    if _CLARIFY.search(t) and _REF.search(t):
        return CLARIFY
    if _CONSULTA.search(t) and not _FAILURE.search(t):
        return CONSULTA
    if _GUIDE.search(t) or _CLARIFY.search(t):
        return GUIDE
    return DIAGNOSTIC


_TECH_STRONG = re.compile(r"\b(ipconfig|dhcp|dns|ping|gateway)\b", re.I)


def is_technical(text: str) -> bool:
    return bool(_TECH.search(text)) or classify_intent(text) == OUTPUT


def audience(setting: str, user_texts: list[str]) -> str:
    """'equilibrado' | 'basico' | 'tecnico'. `auto` = balanced, sticky-technical once technical vocabulary appears."""
    if setting in ("basico", "tecnico"):
        return setting
    return "tecnico" if any(is_technical(t) for t in user_texts) else "equilibrado"


# Tool names offered per intent when conditional tool offering is on.
_ALWAYS = ("create_ticket",)


def tool_exclusions(intent: str, all_names: list[str], text: str) -> set[str]:
    keep: set[str] = set()
    if intent == TICKET:
        keep = {"get_ticket_status", "create_ticket"}
    elif intent == INVENTORY:
        keep = {"get_network_inventory", "get_network_details"}
    elif intent == OUTPUT:
        keep = {"parse_command_output", "create_ticket"}
    elif intent == DIAGNOSTIC:
        keep = {"create_ticket"}  # CLARIFY/GUIDE/SMALLTALK: no tools
    return {n for n in all_names if n not in keep}


# ------------------------------------------------------------------ retrieval query
_DISCONNECT = re.compile(
    r"se (me )?(fue|cay[oó]|cae|corta|cort[oó]|va)|sin (internet|wifi|wi-fi|conexi[oó]n)|no (tengo|hay) (internet|wifi|wi-fi)|se desconect|se pierde",
    re.I,
)


_PHYS = re.compile(
    r"\bluz\b|\bluces\b|\bleds?\b|\bparpadea\w*|\bLOS\b|\bPON\b|\bfibra\b|\bsin se[nñ]al del proveedor\b",
    re.I,
)
_NEG_PASS = re.compile(
    r"\bno\b[^.?!,;]{0,25}\b(clave|contrase[nñ]a|password)\b|\bsin (cambiar|tocar)[^.?!,;]{0,15}\b(clave|contrase[nñ]a)\b",
    re.I,
)
_PASS = re.compile(r"\b(clave|contrase[nñ]a|password|autentic\w*|rechaza\w*|incorrecta)\b", re.I)
_PHYS_BOOST = (
    "módem router luz roja apagada parpadea sin señal del proveedor corte del servicio revisar cables reiniciar"
)


def strip_negated_password(text: str) -> str:
    return _NEG_PASS.sub(" ", text)


def password_mentioned(text: str) -> bool:
    """True only for a real password complaint; «no he cambiado la clave» is not one."""
    return bool(_PASS.search(strip_negated_password(text)))


def is_physical(text: str) -> bool:
    return bool(_PHYS.search(text))


_APIPA = re.compile(
    r"\b169\.254\.\d{1,3}\.\d{1,3}\b|\bapipa\b|no obtiene (una )?(ip|direcci[oó]n)|sin direcci[oó]n ip", re.I
)
_APIPA_BOOST = (
    "APIPA 169.254 DHCP el equipo no obtuvo dirección IP del router; caso NET-003; "
    "reiniciar router, ipconfig /release, ipconfig /renew, renovar concesión"
)
_APIPA_UP = re.compile(r"NET-003|dhcp|apipa|169\.254", re.I)
_APIPA_DOWN = re.compile(r"flushdns|cach[eé] dns|\bdns\b|proxy|ejecutar comandos de red|paso \d", re.I)


def is_apipa(text: str) -> bool:
    return bool(_APIPA.search(text))


def expand_query(text: str) -> str:
    """Keyword boost so colloquial outages / physical symptoms hit connectivity and modem content, not auth content."""
    q = strip_negated_password(text).strip() or text
    if is_apipa(q):
        return f"{q}\n{_APIPA_BOOST}"
    if is_physical(q):
        return f"{q}\n{_PHYS_BOOST}"
    if _DISCONNECT.search(q):
        return f"{q}\nWi-Fi se desconecta o se cae la conexión, sin internet, módem router luces"
    return q


_OUTAGE_TITLE = re.compile(
    r"interrump|desconect|no tiene internet|sin internet|acceso externo|se cae|intermitent|conectividad|luces?|m[oó]dem|proveedor|f[ií]sic|apagad|sin se[nñ]al",
    re.I,
)
_AUTH_TITLE = re.compile(r"autentic|contrase[nñ]a|clave|password|wpa", re.I)


def outage_boost(text: str, title_of: list[str], body_of: list[str] | None = None) -> list[int]:
    """Indices (stable) with outage/modem content first; for physical symptoms without a real password complaint,
    authentication/password content is dropped (kept only if nothing else remains)."""
    idx = list(range(len(title_of)))
    if is_apipa(text):  # APIPA: NET-003 + DHCP guide first; DNS-cache/flushdns/proxy content last
        body = body_of or [""] * len(title_of)

        def rank(i: int) -> int:
            if _APIPA_UP.search(title_of[i]):
                return 0
            return 2 if _APIPA_DOWN.search(f"{title_of[i]} {body[i][:600]}") else 1

        return sorted(idx, key=rank)
    phys = is_physical(text)
    if not phys and not _DISCONNECT.search(text):
        return idx
    if phys and not password_mentioned(text):
        rest = [i for i in idx if not _AUTH_TITLE.search(title_of[i])]
        idx = rest or idx
    return sorted(idx, key=lambda i: 0 if _OUTAGE_TITLE.search(title_of[i]) else 1)


_OFFER = re.compile(
    r"\b(ticket|tickets)\b|quiere que (lo |le |se )?(registre|cree|abra|deje registrado)|\b(registr\w+|abr\w+|crear?|dejar registrado)\b[^.?!]{0,25}\b(problema|caso|reporte|solicitud)\b|reporte para (un|el) t[eé]cnico|\bun t[eé]cnico (lo|lo revise|revise)\b",
    re.I,
)
_PERSISTS = re.compile(
    r"sigue (igual|sin|fallando|sin funcionar)|sigo sin|no (me )?(funcion[oó]|sirvi[oó]|resolvi[oó]|result[oó]|ayud[oó])|ya (lo |la |los |las |me )?(reinici|prob|intent|hice|revis|desenchuf|apagu|desconect|hecho)|sigue la luz|persiste|contin[uú]a (igual|sin)|todav[ií]a (no|sigue)|a[uú]n (no|sigue)|nada (funcion|cambi)|lo mismo",
    re.I,
)
_ASKS_HUMAN = re.compile(
    r"\bt[eé]cnico|\bhumano|\basesor|\balguien (que|de)\b|\buna persona\b|\bsoporte (humano|t[eé]cnico)|hablar con (una |un )?(persona|agente)|\bque (me )?(llame|visite)",
    re.I,
)


def offered_ticket(assistant_text: str) -> bool:
    return bool(_OFFER.search(assistant_text or ""))


def ticket_decision(text: str, snap: dict, offered_before: list[bool], has_prior_answer: bool) -> bool:
    """Priority: (1) user asks for a person -> offer; (2) problem persists after tried steps -> ESCALATE (even if
    offered last turn); (3) abnormal light + restart -> offer; (4) otherwise once per session, never twice in a row."""
    if _ASKS_HUMAN.search(text):
        return True
    keys = {f["key"] for f in snap["facts"]} | set(snap.get("step_keys", []))
    if _PERSISTS.search(text) and bool(snap.get("steps_tried") or has_prior_answer):
        return True
    if offered_before and offered_before[-1]:
        return False
    if "modem_abnormal_light" in keys and "restarted" in keys:
        return True
    return has_prior_answer and not any(offered_before)


_STEP = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.+)$", re.M)


def last_step(assistant_text: str) -> str | None:
    """Last numbered/bulleted step (or backticked command) of the previous assistant message."""
    if not assistant_text:
        return None
    steps = _STEP.findall(assistant_text)
    if steps:
        return _MD.sub("", steps[-1]).strip()[:240]
    cmds = re.findall(r"`([^`\n]{2,60})`", assistant_text)
    return f"escribir el comando {cmds[-1]}" if cmds else None


# ------------------------------------------------------------------ rolling case state (deterministic, no LLM)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# (key, kind, regex, label shown in the UI, text injected in the prompt). kind: fact | step
_SPEC: list[tuple[str, str, re.Pattern[str], str, str]] = [
    (
        "modem_abnormal_light",
        "fact",
        re.compile(r"luz (roja|naranja|amarilla)|luces? (rojas?|apagadas?)|sin luces|luz.*(parpadea|apagad)", re.I),
        "Luz anormal en el módem",
        "el módem/router muestra una luz anormal (roja u otra)",
    ),
    (
        "connection_lost",
        "fact",
        re.compile(
            r"se (me )?(fue|cay[oó]|cae|cort[oó]|corta)|sin (internet|wifi|wi-fi)|no (navega|tengo internet|hay internet)",
            re.I,
        ),
        "Perdió la conexión a internet",
        "perdió la conexión a internet",
    ),
    (
        "other_devices_ok",
        "fact",
        re.compile(
            r"(otros|los dem[aá]s) (equipos|dispositivos|celulares|computadores|pcs?)[^.]*\bs[ií]\b|otros equipos s[ií] navegan",
            re.I,
        ),
        "Otros equipos sí navegan",
        "otros equipos SÍ navegan (el problema es solo de este equipo)",
    ),
    (
        "no_device_online",
        "fact",
        re.compile(r"(ning[uú]n|ninguno de los) (otro )?(equipo|dispositivo)|nadie (navega|tiene)", re.I),
        "Ningún equipo navega",
        "ningún equipo navega (el problema está en el módem o el proveedor)",
    ),
    (
        "wifi_password_unchanged",
        "fact",
        re.compile(r"no (he )?cambiad[oa] (la )?(clave|contrase[nñ]a)", re.I),
        "No cambió la clave del Wi-Fi",
        "no ha cambiado la clave del Wi-Fi",
    ),
    (
        "restarted",
        "step",
        re.compile(r"\b(reinici|desenchuf|desconect[eé]|apag[uú]e|apagu[eé]|prend[ií])", re.I),
        "reinició el módem",
        "reinició el módem",
    ),
    (
        "reconnected",
        "step",
        re.compile(r"\b(reconect|olvid[eé] la red|modo avi[oó]n)", re.I),
        "Se reconectó a la red",
        "se reconectó a la red",
    ),
    (
        "cables_checked",
        "step",
        re.compile(r"cables? (est[aá]n?|revis|verific)|revis[eé] (los )?cables|cables? bien", re.I),
        "Revisó los cables",
        "revisó los cables",
    ),
    (
        "ip_renewed",
        "step",
        re.compile(r"ipconfig\s*/(release|renew)|renov[eé] la ip|liber[eé] la ip", re.I),
        "Liberó/renovó la IP",
        "ejecutó ipconfig /release y /renew",
    ),
    (
        "needs_guidance",
        "fact",
        re.compile(r"no s[eé] c[oó]mo|no entiendo", re.I),
        "Necesita instrucciones paso a paso",
        "necesita instrucciones paso a paso, sin tecnicismos",
    ),
]
_PROMPT = {k: p for k, _, _, _, p in _SPEC}
_PROMPT_TRIED = {k: p for k, kind, _, _, p in _SPEC if kind == "step"}


def case_snapshot(user_texts: list[str], audience_level: str | None = None, last_intent: str | None = None) -> dict:
    """Structured case state (regex-extracted, no LLM): facts, steps tried, IPs, which devices are affected."""
    blob = "\n".join(user_texts)
    facts, steps = [], []
    for key, kind, rx, label, _ in _SPEC:
        if rx.search(blob):
            (facts if kind == "fact" else steps).append({"key": key, "label": label, "value": True})
    keys = {f["key"] for f in facts}
    devices = (
        "solo este equipo" if "other_devices_ok" in keys else "ningún equipo" if "no_device_online" in keys else None
    )
    return {
        "facts": facts,
        "steps_tried": [s["label"] for s in steps],
        "step_keys": [s["key"] for s in steps],
        "ips": list(dict.fromkeys(_IP.findall(blob)))[:2],
        "devices": devices,
        "audience": audience_level,
        "last_intent": last_intent,
        "ticket_offer": None,
        "ticket_offers_before": 0,
    }


def render_case_state(snap: dict) -> str:
    """Prompt block from the snapshot (same text the model always got)."""
    present = {f["key"] for f in snap["facts"]} | set(snap.get("step_keys", []))
    lines = [_PROMPT[k] for k, *_ in _SPEC if k in present]
    if snap["ips"]:
        lines.append("dirección IP que mencionó: " + ", ".join(snap["ips"]))
    if not lines:
        return ""
    return "ESTADO DEL CASO (lo que la persona ya dijo; no se lo vuelva a preguntar):\n" + "\n".join(
        f"- {f}" for f in lines
    )


def tried_instruction(snap: dict | None, apipa: bool = False) -> str:
    """Explicit 'do not repeat' + escalation guidance when the case state already has steps tried."""
    tried = [_PROMPT_TRIED[k] for k in (snap or {}).get("step_keys", []) if k in _PROMPT_TRIED]
    if not tried:
        return ""
    out = (
        f"La persona YA hizo: {', '.join(tried)}. No repita esos pasos; avance al siguiente nivel. "
        "Si el síntoma persiste tras eso, explique que el problema probablemente es del proveedor o de la línea, "
        "sugiera contactar al proveedor y ofrezca registrar el problema."
    )
    return out


def case_state(user_texts: list[str]) -> str:
    return render_case_state(case_snapshot(user_texts))


# ------------------------------------------------------------------ per-turn instruction
_BASIC = (
    "Habla con una persona que no sabe de informática: español sencillo y cálido, sin tecnicismos (di «el módem», «reiniciarlo»); "
    "si un término es inevitable, explícalo entre paréntesis."
)
_BALANCED = (
    "Responde detallado pero amable y fácil de entender: explica qué pasa y por qué; si un término técnico o un comando ayuda, "
    "inclúyelo explicado con palabras sencillas (no lo ocultes)."
)
_TECHNICAL = "La persona es técnica: puedes usar vocabulario técnico y comandos con precisión."
_WHO = {"basico": _BASIC, "equilibrado": _BALANCED, "tecnico": _TECHNICAL}
_TRATO = {
    "usted": "Trato: usted (ejemplo: «Le recomiendo reiniciarlo», «¿Quiere que registre el problema?»). Español neutro de Colombia; nunca uses voseo ni «vosotros» ni «acá».",
    "tu": "Trato: tú (ejemplo: «Te recomiendo reiniciarlo»). Español neutro de Colombia; nunca uses voseo (nada de «querés», «probá») ni «vosotros» ni «acá».",
}
_PHYSICAL = (
    "Orden: primero lo físico y fácil (luces del módem, cables, desenchufarlo 30 segundos, probar otro equipo, acercarse al router); "
    "comandos solo si sigue fallando y guiados clic a clic (Windows+R, escribe cmd, Enter, escribe ipconfig)."
)
_OFFER_YES = "Al final ofrezca registrar el problema para que un técnico lo revise (¿Quiere que lo registre?)."
_OFFER_NO = "No ofrezca ticket en esta respuesta ni mencione registrar el problema."
_APIPA_RULE = "Para 169.254.x.x no recomiende flushdns ni proxy; el problema es que el equipo no obtuvo dirección."
_APIPA_STEPS = (
    "Pasos base para 169.254.x.x, en este orden: (1) ¿otros equipos navegan?; (2) reiniciar el router/módem; (3) reconectar el equipo a la red; "
    "(4) en cmd ejecutar `ipconfig /release` y luego `ipconfig /renew` (explique que sueltan y piden de nuevo una dirección al router); (5) si sigue igual, ofrecer registrar el problema. "
    "Incluya siempre release y renew."
)
_NOINVENT = (
    "Nunca inventes IPs, puertas de enlace ni valores que la persona no dio o una herramienta no devolvió (una dirección 169.254.x.x no tiene puerta de enlace); "
    "para hallarla, indique que la lea en ipconfig, línea «Puerta de enlace predeterminada»."
)
_NATURAL = (
    "Razona por dentro separando lo que dijo la persona de tus suposiciones, pero escribe natural («Por lo que me cuenta…», «Puede que…»); "
    "nunca uses etiquetas como Hecho, Hipótesis o Evidencia, ni digas «contexto» o «fragmentos»."
)
_SOURCES = "Usa las FUENTES con tus palabras, no las copies; cita [n] solo si el dato viene de esa fuente."


def instruction(
    intent: str,
    level: str,
    voice: bool,
    has_sources: bool,
    trato: str = "usted",
    step: str | None = None,
    offer: bool | None = None,
    apipa: bool = False,
    snap: dict | None = None,
) -> str:
    """Per-turn instruction; the form of address goes LAST (a 1.7B model follows the most recent text best)."""
    return (
        _instruction(intent, level, voice, has_sources, step, offer, apipa, snap)
        + " "
        + _TRATO.get(trato, _TRATO["usted"])
    )


def _instruction(
    intent: str,
    level: str,
    voice: bool,
    has_sources: bool,
    step: str | None = None,
    offer: bool | None = None,
    apipa: bool = False,
    snap: dict | None = None,
) -> str:
    who = _WHO.get(level, _BALANCED)
    if intent == CLOSING:
        return f"INSTRUCCIÓN (no la menciones): {who} La persona está agradeciendo o despidiéndose. Despídase en 1 o 2 frases cálidas. Si en esta conversación se creó un ticket, recuerde su número. NO dé pasos, NO explique de nuevo el problema y NO haga preguntas. Sin listas."
    if intent == SMALLTALK:
        return f"INSTRUCCIÓN (no la menciones): {who} Responde con un saludo breve y cálido (máximo 2 frases) y ofrece ayuda con su problema de internet o red. Sin listas. NO ofrezcas registrar ni abrir tickets o casos."
    if intent == TICKET:
        return f"INSTRUCCIÓN (no la menciones): {who} Responde directo y corto (máximo 2 frases) con lo que devolvió la herramienta; no uses formato de diagnóstico ni cites fuentes."
    if intent == INVENTORY:
        return f"INSTRUCCIÓN (no la menciones): {who} Responde directo y corto con lo que devolvió la herramienta; no uses formato de diagnóstico."
    if intent == CLARIFY:
        return (
            f"INSTRUCCIÓN (no la menciones): {who} La persona pide aclarar algo de tu respuesta anterior: responde SOLO a esa pregunta, "
            "en 2 a 4 frases explicativas; no repitas los pasos anteriores ni la lista de pasos; como máximo una pregunta corta al final. "
            + ("Texto plano, sin listas." if voice else "Sin listas largas.")
            + (" Usa las FUENTES con tus palabras; cita [n] solo si el dato viene de ellas." if has_sources else "")
        )
    if intent == GUIDE:
        focus = (
            f"El último paso que se le pidió fue: «{step}». Guíala ÚNICAMENTE en ese paso, en pasos pequeños, uno a uno, clic a clic. "
            if step
            else ""
        )
        return (
            f"INSTRUCCIÓN (no la menciones): {who} La persona no sabe cómo hacerlo: {focus}guíala paso a paso, con calma, con un ejemplo claro, "
            "y nunca le preguntes qué comando quiere usar. En Windows: tecla Windows+R, escribir cmd, Enter, escribir ipconfig, Enter y leer la línea «Dirección IPv4». "
            "Si conviene, ofrece una comprobación más simple sin comandos. Termina con UNA pregunta fácil. "
            + ("Texto plano, máximo 4 frases cortas." if voice else "Puedes usar lista numerada y **negrita**.")
        )
    parts = [f"INSTRUCCIÓN (no la menciones): {who}", _PHYSICAL, _NOINVENT, _NATURAL]
    if apipa:
        parts.append(_APIPA_RULE)
        parts.append(_APIPA_STEPS)
    if tried := tried_instruction(snap):
        parts.append(tried)
    if offer is not None:
        parts.append(_OFFER_YES if offer else _OFFER_NO)
    if has_sources:
        parts.append(_SOURCES)
    if voice:
        parts.append(
            "Responde en texto plano, máximo 4 frases cortas, sin listas ni markdown, y termina con UNA pregunta."
        )
    else:
        parts.append(
            "Formato compacto: un párrafo corto que explique qué pasa probablemente y por qué (sin repetir el síntoma al inicio); hasta 4 pasos numerados cortos; termina con UNA pregunta fácil. Máximo 150 palabras en total. "
            "Puedes usar **negrita** y listas numeradas."
        )
    return " ".join(parts)


# Defaults for the `max_tokens_by_intent` setting (editable from the UI). Generous on purpose: an answer cut by
# max_tokens is worse than a long one; the per-turn instruction already asks for concise answers.
DEFAULT_MAX_TOKENS_BY_INTENT: dict[str, dict[str, int]] = {
    "diagnostic": {"text": 1100, "voice": 600},
    "guide": {"text": 1100, "voice": 600},
    "output": {"text": 1100, "voice": 600},
    "consulta": {"text": 1400, "voice": 600},
    "clarify": {"text": 500, "voice": 500},
    "ticket": {"text": 400, "voice": 400},
    "inventory": {"text": 400, "voice": 400},
    "smalltalk": {"text": 240, "voice": 240},
    "closing": {"text": 160, "voice": 160},
    "plantilla": {"text": 440, "voice": 440},  # deterministic-template phrasing turns (ticket created, follow-up…)
    "extraccion": {"text": 300, "voice": 300},  # LLM fact extraction (JSON)
}


def max_tokens_for(intent: str, voice: bool, default: int = 300, overrides: dict | None = None) -> int:
    """LLM max_tokens for a turn type; `overrides` = the `max_tokens_by_intent` setting (partial dicts allowed)."""
    key = intent if intent in DEFAULT_MAX_TOKENS_BY_INTENT else "diagnostic"
    mode = "voice" if voice else "text"
    for table in (overrides or {}, DEFAULT_MAX_TOKENS_BY_INTENT):
        v = (table.get(key) or {}).get(mode) if isinstance(table.get(key), dict) else None
        if isinstance(v, int) and v > 0:
            return v
    return default


# ------------------------------------------------------------------ post-filter
_LABEL = re.compile(
    r"^\s*(?:[-*•▪]\s*)?(?:\*\*)?(?:Hechos?|Hip[oó]tesis|Evidencias?|Pregunta de seguimiento|Pregunta|Problema(?: detectado)?)(?:\*\*)?\s*:\s*(?:\*\*)?\s*",
    re.I,
)
_INLINE_LABEL = re.compile(r"(?<=[.!?¿])\s*(?:Hechos?|Hip[oó]tesis|Evidencias?)\s*:\s*", re.I)
_INTERNAL = re.compile(
    r"\b(el contexto|contexto proporcionado|fragmentos?|las? fuentes?|el usuario (dijo|indica|menciona|dice|comenta))\b",
    re.I,
)
_CARD = re.compile(
    r"^\s*(?:\[\d+\]\s*)?(Caso\s+[A-Z]+-\d+|S[ií]ntomas\s*:|Preguntas iniciales\s*:|Causas posibles\s*:|FUENTES)", re.I
)
_BAD_GATEWAY = re.compile(
    r"\b(ping|haga ping|haz ping)\b[^.?!]*169\.254\.\d+\.\d+|169\.254\.\d+\.\d+\s*\((?:la )?(?:puerta de enlace|gateway)",
    re.I,
)
_CITE = re.compile(r"\s*\[(\d+)\]")
_PLACEHOLDER = re.compile(r"\s*\[\s*(?:n|x|n[uú]mero|num|fuente|\.\.\.|…)?\s*\]", re.I)
_TICKET_OFFER = re.compile(r"\b(ticket|tickets|registr\w+|reporte|caso)\b", re.I)
_TICKET_NO = re.compile(r"\bTRM-\d{3,}\b", re.I)
_SENT = re.compile(r"(?<=[.!?…])\s+")
_WORD = re.compile(r"\w+", re.U)
_STOP = set(
    "el la los las un una unos unas de del al a en y o u que se su sus es son por para con sin lo le les mi tu me te ya muy mas más como si no sí pero este esta esto ese esa eso hay ha han ser estar puede pueden tiene tienen está están al lo".split()
)
_VOSEO = re.compile(
    r"\b(?:querés|podés|tenés|sabés|decís|hacés|venís|ponés|sos|vos|probá|revisá|fijate|decime|contame|mirá|abrí|reiniciá|"
    r"desenchufá|esperá|intentá|verificá|chequeá|escribí|hacé|andá|(?:desenchuf|reinici|prob|revis|apag|enchuf|cheque|verific|intent)a(?:lo|la|los|las))\b",
    re.I,
)


def voseo_hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _VOSEO.finditer(text)})


_MD = re.compile(r"\*\*|__|`|^#+\s*", re.M)


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall(_norm(s)) if len(w) > 2 and w not in _STOP}


def _words(s: str) -> list[str]:
    return _WORD.findall(_norm(s))


def _verbatim(sentence: str, sources: list[str], n: int = 12) -> bool:
    w = _words(sentence)
    if len(w) < n:
        return False
    grams = {" ".join(w[i : i + n]) for i in range(len(w) - n + 1)}
    for src in sources:
        sw = _words(src)
        if any(" ".join(sw[i : i + n]) in grams for i in range(len(sw) - n + 1)):
            return True
    return False


SUPPORT_MIN = 0.34  # share of the sentence's content words found in the cited source (calibrated on the 3 test turns)


def _supported(sentence: str, source: str, user_text: str) -> bool:
    user = _tokens(user_text)
    st = {t for t in _tokens(_CITE.sub("", sentence)) if not t.isdigit() and t not in user}
    if not st:
        return False
    return len(st & _tokens(source)) / len(st) >= SUPPORT_MIN


def filter_sentence(s: str) -> str:
    """Stateless per-sentence cleanup, also used on the voice path before TTS. Returns '' when the sentence must go."""
    if _CARD.match(s):
        return ""
    s = _LABEL.sub("", s)
    if _INTERNAL.search(s) or _BAD_GATEWAY.search(s):
        return ""
    return s


def dedupe_repeat(text: str) -> str:
    """Two answers glued together (the model restarts after its own question): keep the first coherent one."""
    t = text.strip()
    m = re.search(r"\?(?=[A-ZÁÉÍÓÚ])", t)  # a question mark glued to the next sentence: "...haga?Hecho: ..."
    if m and len(t[m.end() :]) > 30:
        return t[: m.end()]
    return t


_MARK = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")
_ORPHAN = re.compile(r"^\s*(?:\d+[.)]|[-*•])?\s*$")
_NUM = re.compile(r"^(\s*)\d+([.)])\s+")
_LIST_OR_HEAD = re.compile(r"^\s*(?:#+\s|\*\*|\d+[.)]\s|[-*•]\s)")


def _drop_opener(text: str) -> str:
    """Conservative: drop a first line of <= 8 words that only restates the symptom, when a blank line and a heading/list follow."""
    parts = text.split("\n", 2)
    if len(parts) == 3 and parts[1] == "":
        first, rest = parts[0].strip(), parts[2]
        if (
            0 < len(first.split()) <= 8
            and first.endswith(".")
            and not first.startswith(("#", "*", "-"))
            and _LIST_OR_HEAD.match(rest)
        ):
            return rest.strip()
    return text


def _renumber(lines: list[str]) -> list[str]:
    """Numbered items sequential within each consecutive run (a dropped step must not leave a gap)."""
    out, n, in_code = [], 0, False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            out.append(ln)
            continue
        if in_code:  # code blocks are verbatim and do not break a numbered list around them
            out.append(ln)
            continue
        if _NUM.match(ln):
            n += 1
            ln = _NUM.sub(rf"\g<1>{n}\g<2> ", ln, count=1)
        elif ln.strip():
            n = 0
        out.append(ln)
    return out


def clean_answer(
    text: str, sources: list[str], user_text: str, voice: bool, intent: str | None = None, offer: bool | None = None
) -> str:
    """Final answer: no internal labels / context talk / copied cards, validated [n] markers, plain text for voice."""
    text = dedupe_repeat(text)
    text = _INLINE_LABEL.sub(" ", _PLACEHOLDER.sub("", text))
    out_lines: list[str] = []
    in_code = False
    for line in text.split("\n"):
        # Fenced code blocks (YAML, commands) pass through untouched: indentation and content are meaningful.
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out_lines.append(line.rstrip())
            continue
        if in_code:
            out_lines.append(line.rstrip())
            continue
        if not line.strip():
            out_lines.append("")
            continue
        kept: list[str] = []
        if _ORPHAN.match(line):
            continue
        mk = _MARK.match(line)
        marker, body = (mk.group(0), line[mk.end() :]) if mk else ("", line)
        for s in _SENT.split(body):
            f = filter_sentence(s)
            if not f.strip() or _verbatim(f, sources):
                continue
            # Never drop a sentence that reports a real ticket number (e.g. "Se creó el ticket TRM-000002").
            reports_ticket = bool(_TICKET_NO.search(f))
            if intent in (SMALLTALK, CLOSING) and _TICKET_OFFER.search(f) and not reports_ticket:
                continue
            if offer is False and _OFFER.search(f) and not reports_ticket:
                continue

            def fix(m: re.Match[str], f: str = f) -> str:
                n = int(m.group(1))
                ok = 1 <= n <= len(sources) and _supported(f, sources[n - 1], user_text)
                return f" [{n}]" if ok else ""

            f = _CITE.sub(fix, f)
            kept.append(f.strip())
        if kept:
            out_lines.append(marker + " ".join(kept))
    res = re.sub(r"\n{3,}", "\n\n", "\n".join(_renumber(out_lines))).strip()
    res = _drop_opener(res)
    if voice:
        res = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s+", "", _MD.sub("", res), flags=re.M)
        res = re.sub(r"\s*\n+\s*", " ", res)
    return res


# ------------------------------------------------------------------ deterministic ticket creation
_TITLE_BASE = {
    "no_device_online": "Sin internet en ningún equipo",
    "connection_lost": "Pérdida de conexión a internet",
}


def build_ticket(user_texts: list[str], snap: dict) -> dict:
    """Ticket fields from the rolling case state (no LLM): title, description, category, priority, evidence."""
    keys = {f["key"] for f in snap["facts"]} | set(snap.get("step_keys", []))
    labels = [f["label"] for f in snap["facts"]]
    blob = " ".join(user_texts)
    base = next((v for k, v in _TITLE_BASE.items() if k in keys), "Problema de conectividad")
    if "modem_abnormal_light" in keys:
        base = "Sin internet: luz anormal en el módem" + (" tras reiniciar" if "restarted" in keys else "")
    if re.search(r"wi-?fi|inal[aá]mbric", blob, re.I) and not keys & {"modem_abnormal_light", "no_device_online"}:
        category = "wifi"
    elif re.search(r"169\.254|dhcp|ipconfig", blob, re.I):
        category = "dhcp"
    elif re.search(r"\bdns\b|nslookup", blob, re.I):
        category = "dns"
    elif re.search(r"latencia|lent[oa]|ping", blob, re.I):
        category = "latency"
    elif re.search(r"cable|ethernet", blob, re.I):
        category = "ethernet"
    else:
        category = "connectivity"
    provider_level = bool(keys & {"modem_abnormal_light", "no_device_online"}) and "restarted" in keys
    last = [t.strip()[:160] for t in user_texts[-4:] if t.strip()]
    desc = [f"Hechos: {'; '.join(labels) or 'sin detalle registrado'}."]
    if snap.get("steps_tried"):
        desc.append(f"Pasos ya probados: {'; '.join(snap['steps_tried'])}.")
    if snap.get("ips"):
        desc.append(f"IP mencionada: {', '.join(snap['ips'])}.")
    desc.append("Últimos mensajes del usuario: " + " | ".join(last))
    return {
        "title": base[:200],
        "description": " ".join(desc)[:4000],
        "category": category,
        "priority": "high" if provider_level else "medium",
        "evidence": [f"{x}" for x in [*labels, *snap.get("steps_tried", [])]][:20],
    }


def ticket_created_instruction(number: str, priority: str, created: bool, trato: str = "usted") -> str:
    if created:
        body = (
            f"Se creó el ticket {number} con prioridad {priority}. Confirme al usuario en 2–3 frases, dé el número, "
            "explique qué sigue (un técnico lo contactará; mientras tanto puede contactar a su proveedor) y no repita los pasos."
        )
    else:
        body = (
            f"Ya existe el ticket {number} para esta conversación (prioridad {priority}). Dígaselo en 1–2 frases con el número, "
            "explique que un técnico lo contactará, no cree otro ni repita los pasos."
        )
    return f"INSTRUCCIÓN (no la menciones): {body} Texto plano, sin listas, sin ofrecer más tickets. " + _TRATO.get(
        trato, _TRATO["usted"]
    )
