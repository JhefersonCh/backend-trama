# ruff: noqa: E501
"""Agent turn policy (pure, no I/O): perception regexes -> catalog facts, select_action (spec §3.4), K3 slot
parsing, contact redaction and the deterministic templates (A1, A3, K4, why). The LLM only phrases what this decides."""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent.explain import cf_word

from . import policy

# ------------------------------------------------------------------ new intents
WHY, TICKET_FOLLOWUP = "why", "ticket_followup"

_WHY = re.compile(
    r"\bpor\s*qu[eé]\s+(me\s+|le\s+|te\s+)?(pregunt\w*|cree\w*|crees|piens\w*|dice\w*|dijo|sospech\w*|descart\w*|asegur\w*|est[aá]\s+(tan\s+)?segur\w*)"
    r"|\bc[oó]mo\s+(lo\s+)?sab(e|es|en)\b|\bpor\s*qu[eé]\s+(es|ser[ií]a|seria)\s+(el|la|un|una|del)\b|\ben\s+qu[eé]\s+se\s+basa",
    re.I,
)
_FOLLOWUP = re.compile(
    r"\bmi\s+(ticket|caso|reporte|solicitud)\b|\bqu[eé]\s+pas[oó]\s+con\b|\bc[oó]mo\s+va\b|\bya\s+vino\s+el\s+t[eé]cnico|"
    r"\b(cu[aá]ndo|a\s+qu[eé]\s+hora)\s+(viene|llega|vendr[aá])\s+el\s+t[eé]cnico|\bestado\s+de\s+(mi|el)\s+(caso|ticket|reporte)|"
    r"\b(no\s+ha\s+venido|no\s+ha\s+llegado)\s+(el|ning[uú]n)\s+t[eé]cnico",
    re.I,
)
_CREATE_VERB = re.compile(r"\b(abr\w+|cre\w+|gener\w+|registr\w+|levant\w+|hac\w+|haz)\b", re.I)
TICKET_NO = re.compile(r"\bTRM-\d+\b", re.I)


def refine_intent(text: str, intent: str) -> str:
    """WHY / TICKET_FOLLOWUP take precedence over the base router (never over an explicit TRM- number lookup)."""
    t = text.strip()
    if len(t) <= 200 and _WHY.search(t):
        return WHY
    if len(t) <= 200 and _FOLLOWUP.search(t) and not TICKET_NO.search(t) and not _CREATE_VERB.search(t):
        return TICKET_FOLLOWUP
    return intent


# ------------------------------------------------------------------ perception (regex -> catalog facts)
def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


_LIGHT = re.compile(
    r"\b(?:luz|luces|led|bombillo|foco)\b[^.?!]{0,30}?\b(roja|rojo|rojas|naranja|amarill[ao]|apagad[ao]s?|verde|verdes|parpade\w*|titil\w*)",
    re.I,
)
_LIGHT_ALT = re.compile(r"\b(sin luces|no (tiene|prende|enciende) (ninguna )?luz|no prende|no enciende)\b", re.I)
_LIGHT_MAP = {
    "roj": "roja",
    "nar": "naranja",
    "ama": "naranja",
    "apa": "apagada",
    "ver": "verde",
    "par": "parpadea",
    "tit": "parpadea",
}
_RESTART = re.compile(r"\b(reinici\w*|desenchuf\w*|lo apagu[eé]|apagu[eé] y prend\w*|lo desconect[eé])", re.I)
_STILL_RED = re.compile(
    r"\b(sigue|contin[uú]a|qued[oó]|segu[ií]a)\b[^.?!]{0,25}\b(roj[ao]|naranja|igual)\b|\bluz roja sigue|\bsigue (con )?la luz",
    re.I,
)
_LIGHT_OK = re.compile(
    r"\b(qued[oó]|est[aá]|ya est[aá]|volvi[oó])\b[^.?!]{0,20}\b(verde|normal|bien)\b|\bluz (verde|normal)\b", re.I
)
_ALL_GREEN = re.compile(
    r"\btod[oa]s?\b[^.?!]{0,30}\b(luz|luces|leds?|prendid\w*|encendid\w*)\b[^.?!]{0,30}\bverdes?\b|\btod[oa]s?\b[^.?!]{0,20}\ben verde\b",
    re.I,
)
_DEV_PHONE = re.compile(r"\b(celular(?:es)?|telefono|movil|smartphone|iphone|android)\b", re.I)
_DEV_PC = re.compile(r"\b(computador\w*|pc|portatil|laptop|notebook|ordenador)\b", re.I)
_SOLO_EN = re.compile(r"\b(solo|s[oó]lo|unicamente|[uú]nicamente)\b[^.?!]{0,15}\b(en|con)\b|\bmi\b", re.I)
_REDUNDANT_AFTER_RESTART = {"guide_reinicio", "ask_luz_modem", "ask_luz_tras_reinicio", "guide_revisar_cable"}


REDO_DIAGRAM = re.compile(
    r"\b(haz|hacer|arma|armar|dibuja|dibujar|genera|generar|muestra|mu[eé]strame|rehaz|rehacer)\b[^.?!]{0,25}\b(diagrama|esquema|mapa)\b[^.?!]{0,40}\b(otra vez|de nuevo|nuevamente)\b"
    r"|\b(otra vez|de nuevo|nuevo)\b[^.?!]{0,25}\b(diagrama|esquema)\b"
    r"|\b(vuelv\w*|volver)\s+a\s+(hacer|armar|dibujar|generar|mostrar)\b[^.?!]{0,30}\b(diagrama|esquema|mapa)\b"
    r"|\b(diagrama|esquema)\b[^.?!]{0,30}\b(otra vez|de nuevo|pero bien|bien hecho|corregido|actualizado)\b",
    re.I,
)


def extract_device(text: str) -> str | None:
    """Which device the user says is failing: 'celular' | 'pc' | None (regex only)."""
    t = _norm(text)
    ph, pc = _DEV_PHONE.search(t), _DEV_PC.search(t)
    if ph and (not pc or ph.start() < pc.start()) and (_SOLO_EN.search(t) or "no tengo internet" in t or "no tiene internet" in t):
        return "celular"
    if pc and not ph:
        return "pc"
    return None


_OTHERS_OK = re.compile(
    r"\b(otros|otras|los dem[aá]s|las dem[aá]s|el celular|mi celular|los celulares|el otro)\b[^.?!]{0,40}\b(s[ií]|tambi[eé]n)\b[^.?!]{0,15}\b(navega\w*|funciona\w*|tiene\w*|conecta\w*|sirve\w*|anda\w*)|"
    r"\b(otros|los dem[aá]s)\b[^.?!]{0,30}\bs[ií]\b|\bsolo (es |pasa )?(en |con )?(este|mi) (equipo|computador|pc|port[aá]til)",
    re.I,
)
_OTHERS_KO = re.compile(
    r"\b(ning[uú]n|ninguno|ninguna)\b[^.?!]{0,20}\b(equipo|dispositivo|celular|computador|aparato)|\bnadie (navega|tiene|puede)|"
    r"\b(otros|los dem[aá]s|el celular|los celulares)\b[^.?!]{0,30}\b(tampoco|no (navega|tiene|funciona|conecta|sirve|anda))|\btodos (los equipos )?(est[aá]n )?sin\b",
    re.I,
)
_PASS_UNCHANGED = re.compile(r"\bno (he )?(cambi\w*|toqu\w*)\b[^.?!]{0,10}\b(la )?(clave|contrase[nñ]a)", re.I)
_PASS_CHANGED = re.compile(
    r"\b(cambi[eé]|cambiamos|cambiaron|cambi[oó])\b[^.?!]{0,10}\b(la )?(clave|contrase[nñ]a)", re.I
)
_PASS_ERROR = re.compile(
    r"\b(clave|contrase[nñ]a|password)\b[^.?!]{0,25}\b(incorrecta|inv[aá]lida|err[oó]nea|mala|no (la )?acepta)|\brechaza\w*\b[^.?!]{0,20}\b(clave|contrase[nñ]a)",
    re.I,
)
_CABLE_OK = re.compile(
    r"\b(por|con) (el )?cable\b[^.?!]{0,25}\bs[ií]\b|\b(por|con) (el )?cable\b[^.?!]{0,15}\b(funciona|navega|sirve|anda)",
    re.I,
)

_YES = re.compile(r"^\W*(s[ií]|sip|claro|correcto|as[ií] es|exacto|afirmativo|efectivamente)\b", re.I)
_NO = re.compile(r"^\W*(no|nop|negativo|para nada|tampoco|ninguno)\b", re.I)

_WORKS = re.compile(
    r"\bya\s+(me\s+|nos\s+)?(funciona|sirve|tengo\s+internet|hay\s+internet|navega|conecta|carga|abre|anda|mejor[oó]|qued[oó]|est[aá]\s+(bien|funcionando))|"
    r"\b(se\s+)?(arregl[oó]|solucion[oó]|resolvi[oó])\b|\bvolvi[oó]\s+(el\s+internet|la\s+conexi[oó]n|a\s+funcionar)|"
    r"\bfuncion[oó]\b|^\W*s[ií],?\s+(ya\s+)?funciona|\bya\s+todo\s+bien|\bqued[oó]\s+(funcionando|resuelto)",
    re.I,
)
_NOT_WORKS = re.compile(
    r"\bno\s+(me\s+)?(funcion|sirv|arregl|solucion|resolv|naveg|conect)|\bsigue|\btodav[ií]a\s+no|\ba[uú]n\s+no|\bigual\b",
    re.I,
)


def is_yes(text: str) -> bool:
    t = text.strip()
    return len(t) <= 80 and bool(_YES.search(t)) and not _NO.search(t)


def is_no(text: str) -> bool:
    t = text.strip()
    return len(t) <= 80 and bool(_NO.search(t))


# "el computador SÍ funciona, pero mi celular no abre" / "en la sala sí funciona": a contrast, not a fix.
_CONTRAST = re.compile(
    r"\bpero\b|\bsolo\b|\bsolamente\b|\bproblema\b|\blent\w*|\bno\s+(me\s+)?(abre|carga|anda|deja|entra)\b|"
    r"\b(otros|otras|los\s+dem[aá]s|las\s+dem[aá]s|en\s+la\s+sala|en\s+el\s+cuarto)\b",
    re.I,
)


def works_signal(text: str) -> bool:
    """User says it works now ("ya funciona", "se arregló") -- not a comparison with other devices or rooms."""
    return bool(_WORKS.search(text)) and not _NOT_WORKS.search(text) and not _CONTRAST.search(text)


def failing_signal(text: str) -> bool:
    return bool(_NOT_WORKS.search(text) or policy._PERSISTS.search(text))


_PING_OK = re.compile(
    r"ping\s+(a\s+)?(8\.8\.8\.8|1\.1\.1\.1)[^.?!]{0,30}(funciona|responde|sin problema|bien)|\bhay ping\b|\bel ping (funciona|responde)", re.I
)
_NAMES_KO = re.compile(r"no se encuentra el servidor|DNS_PROBE|server not found", re.I)
_PAGES_KO = re.compile(r"no cargan (las )?p[aá]ginas|no abre(n)? (las )?p[aá]ginas", re.I)
_IP_CONFLICT = re.compile(r"conflicto de (direcci[oó]n )?ip|direcci[oó]n ip duplicada|ip duplicada", re.I)
_CABLE_OFF = re.compile(r"cable( de red)? desconectado|cable de red (est[aá] )?desconectado", re.I)
_SSID_GONE = re.compile(r"\b(red|wi-?fi) (desapareci[oó]|no aparece)|no sale (mi|la) red", re.I)
_MANY_NETS = re.compile(r"\b(muchas|much[ií]simas) redes", re.I)
_B24 = re.compile(r"2[.,]4 ?ghz", re.I)
_B5 = re.compile(r"(?<![\d.,])5 ?ghz", re.I)
_PEAK = re.compile(r"\b(en las|por la|de) noches?\b|horas pico", re.I)
_SLOW = re.compile(r"lent[oa]|lent[ií]sim", re.I)
_LOSS = re.compile(r"\d+ ?% de p[eé]rdida|p[eé]rdida de paquetes", re.I)
_LAN_HOST = re.compile(r"router|gateway|puerta de enlace|local|192\.168", re.I)
_LAT = re.compile(r"\d{3} ?ms", re.I)
_PASS_CHG2 = re.compile(r"cambi[eé] (la )?(contrase[nñ]a|clave)", re.I)
_NO_CONNECT = re.compile(r"no se (puede )?conecta|no (me )?conecta|no puedo conectar", re.I)


def _extract_extra(t: str, out: dict[str, Any]) -> None:
    ping_ok = bool(_PING_OK.search(t))
    if ping_ok:
        out["navega_por_ip"] = True
    if _NAMES_KO.search(t) or (ping_ok and _PAGES_KO.search(t)):
        out["resuelve_nombres"] = False
        if _NAMES_KO.search(t) and re.search(r"\bping\b", t, re.I) and ping_ok:
            out["navega_por_ip"] = True
    if _IP_CONFLICT.search(t):
        out["conflicto_ip"] = True
    if _CABLE_OFF.search(t):
        out["cable_desconectado"] = True
    if _SSID_GONE.search(t):
        out["ssid_visible"] = False
    if _MANY_NETS.search(t):
        out["muchas_redes_vecinas"] = True
    if _B24.search(t):
        out["banda"] = "24ghz"
    elif _B5.search(t):
        out["banda"] = "5ghz"
    if _PEAK.search(t) and _SLOW.search(t):
        out["lento_horas_pico"] = True
    if _LOSS.search(t) and _LAN_HOST.search(t):
        out["perdida_paquetes_local"] = True
    if _LAT.search(t) and re.search(r"router|gateway|puerta de enlace", t, re.I):
        out["latencia_alta_local"] = True
    if _PASS_CHG2.search(t) and _NO_CONNECT.search(t):
        out["clave_cambiada"] = True
        out["error_contrasena"] = True


def extract_facts(
    text: str, last_action: str | None, actions: dict[str, dict], restart_guided: bool = False
) -> list[tuple[str, Any]]:
    """Catalog facts from one user message (regex only). A plain yes/no answers the LAST ask_* action."""
    out: dict[str, Any] = {}
    t = text
    if m := _LIGHT.search(t):
        color = _LIGHT_MAP.get(_norm(m.group(1))[:3], "desconocida")
        out["luz_modem"] = color
    elif _LIGHT_ALT.search(t):
        out["luz_modem"] = "apagada"
    if _ALL_GREEN.search(t):
        out["luz_modem"] = "verde"
    restarted = bool(_RESTART.search(t)) and not re.search(r"\bno (lo )?(he )?reinici", t, re.I)
    if restarted:
        out["reinicio_hecho"] = True
    if (restarted or last_action in ("ask_luz_tras_reinicio", "guide_reinicio")) and (
        _STILL_RED.search(t) or out.get("luz_modem") in ("roja", "naranja")
    ):
        out["luz_tras_reinicio"] = "roja"
    elif (last_action in ("ask_luz_tras_reinicio", "guide_reinicio") or restart_guided) and (
        _LIGHT_OK.search(t) or out.get("luz_modem") == "verde"
    ):
        out["luz_tras_reinicio"] = "normal"
    if _OTHERS_KO.search(t):
        out["otros_equipos_navegan"] = False
    elif _OTHERS_OK.search(t):
        out["otros_equipos_navegan"] = True
    if policy.is_apipa(t):
        out["ip_apipa"] = True
    if _PASS_UNCHANGED.search(t):
        out["clave_cambiada"] = False
    elif _PASS_CHANGED.search(t):
        out["clave_cambiada"] = True
    if _PASS_ERROR.search(t):
        out["error_contrasena"] = True
    if _CABLE_OK.search(t):
        out["funciona_por_cable"] = True
    _extract_extra(t, out)
    # yes/no to the last question
    spec = actions.get(last_action or "") or {}
    fact = spec.get("fact") if spec.get("kind") == "ask" else None
    if fact and fact not in out:
        if is_yes(t):
            yes_val = {"luz_tras_reinicio": None, "luz_modem": None}.get(fact, True)
            if yes_val is not None:
                out[fact] = yes_val
        elif is_no(t):
            no_val = {"luz_tras_reinicio": None, "luz_modem": None}.get(fact, False)
            if no_val is not None:
                out[fact] = no_val
    return list(out.items())


# ------------------------------------------------------------------ K3: assignment slots
ZONE_LABELS = {
    "mocoa": "Mocoa",
    "puerto_asis": "Puerto Asís",
    "sibundoy": "Sibundoy",
    "orito": "Orito",
    "valle_del_guamuez": "Valle del Guamuez",
    "villagarzon": "Villagarzón",
}
MODE_LABELS = {"visita": "una visita", "remoto": "atención remota"}
SLOT_LABELS = {
    "manana": "en la mañana",
    "tarde": "en la tarde",
    "noche": "en la noche",
    "cualquiera": "en cualquier horario",
}
SLOTS = ("zona", "modalidad", "franja", "contacto")
_SKIP = re.compile(
    r"\bno\s+s[eé]\b|\bno\s+quiero\b|\bprefiero\s+no\b|\bno\s+(lo\s+)?s[eé]\b|\bomit\w*|\bsin\s+contacto\b|\bno\s+tengo\b|\bpaso\b|\bno\s+gracias\b|\bno\s+es\s+necesario\b|^\W*no\W*$",
    re.I,
)
_MODE_VISIT = re.compile(
    r"\bvisit\w*|\bque\s+venga|\ba\s+(la\s+|mi\s+)?casa\b|\bpresencial|\bdomicilio|\ben\s+persona\b", re.I
)
_MODE_REMOTE = re.compile(
    r"\bremot\w*|\btel[eé]fon\w*|\bllamad\w*|\bll[aá]m\w+|\bvirtual\w*|\bpor\s+chat\b|\ba\s+distancia\b", re.I
)
_SLOT = (
    (
        "cualquiera",
        re.compile(
            r"\bcualquier\w*|\bme\s+da\s+(igual|lo\s+mismo)|\bindiferente|\bcuando\s+(sea|puedan|pueda)\b|\btodo\s+el\s+d[ií]a",
            re.I,
        ),
    ),
    ("manana", re.compile(r"\bma[nñ]ana\b|\btemprano\b|\b(a\.?\s?m\.?)\b", re.I)),
    ("tarde", re.compile(r"\btarde\b|\b(p\.?\s?m\.?)\b", re.I)),
    ("noche", re.compile(r"\bnoche\b", re.I)),
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(
    r"(?<![\w.\-])(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{3}\)?[\s-]?)\d{3}[\s-]?\d{4}(?![\w.])|(?<![\w.\-])(?:3\d{9}|60\d{8})(?![\w.])"
)
CONTACT_PLACEHOLDER = "[contacto registrado]"


def parse_zone(text: str, zones: list[str]) -> str | None:
    n = re.sub(r"[^a-z0-9]+", " ", _norm(text))
    for z in zones:
        words = [w for w in re.split(r"[_\s]+", _norm(z)) if w]
        if words and re.search(r"\b" + r"\s*".join(map(re.escape, words)) + r"\b", n):
            return z
    return None


def parse_mode(text: str) -> str | None:
    if _MODE_VISIT.search(text):
        return "visita"
    if _MODE_REMOTE.search(text):
        return "remoto"
    return None


def parse_slot(text: str) -> str | None:
    for key, rx in _SLOT:
        if rx.search(text):
            return key
    return None


def is_skip(text: str) -> bool:
    return bool(_SKIP.search(text.strip()))


def find_contact(text: str) -> str | None:
    m = _EMAIL.search(text) or _PHONE.search(text)
    return m.group(0).strip() if m else None


def redact_contact(text: str) -> tuple[str, str | None]:
    """(text with every phone/e-mail replaced by the placeholder, first contact found or None)."""
    found = find_contact(text)
    if not found:
        return text, None
    red = _PHONE.sub(CONTACT_PLACEHOLDER, _EMAIL.sub(CONTACT_PLACEHOLDER, text))
    return red, found


def zone_label(z: str | None) -> str:
    return ZONE_LABELS.get(z or "", (z or "").replace("_", " ").title())


def missing_slots(questions: list[str], known: dict[str, Any], asked: set[str]) -> list[str]:
    """Slots still worth asking, in configured order; each is asked at most once; contacto only for visita/remoto."""
    out = []
    for q in questions:
        if q not in SLOTS or f"k3:{q}" in asked:
            continue
        if q == "contacto":
            if known.get("modalidad") in ("visita", "remoto"):
                out.append(q)
            continue
        if known.get(q) is None:
            out.append(q)
    return out


def slot_question(slot: str, zones: list[str], known: dict[str, Any]) -> str:
    if slot == "zona":
        names = ", ".join(zone_label(z) for z in zones[:6])
        return f"Para enviar al técnico indicado, ¿en qué municipio está? Por ejemplo: {names}."
    if slot == "modalidad":
        return "¿Prefiere que el técnico le ayude de forma remota (por teléfono) o con una visita?"
    if slot == "franja":
        what = "la visita" if known.get("modalidad") == "visita" else "la atención"
        return f"¿En qué horario prefiere {what}: en la mañana, en la tarde o en la noche? Si le da igual, dígame «cualquiera»."
    return "Si quiere, déjeme un número o correo para que el técnico lo contacte; es opcional."


def confirm_summary(known: dict[str, Any]) -> str:
    mode = known.get("modalidad")
    parts = ["¿Registro el caso para"]
    parts.append(MODE_LABELS.get(mode, "que un técnico lo revise") if mode else "que un técnico lo revise")
    if known.get("zona"):
        parts.append(f"en {zone_label(known['zona'])}")
    if known.get("franja"):
        parts.append(SLOT_LABELS.get(known["franja"], ""))
    return " ".join(p for p in parts if p).strip() + "?"


# ------------------------------------------------------------------ DECIDIR (spec §3.4)
ASK_TEXT = {
    "ask_otros_equipos": "¿Los otros equipos de la casa (celulares, computadores) tienen internet?",
    "ask_luz_modem": "¿De qué color está la luz de internet del módem: verde, roja, naranja, apagada o parpadeando?",
    "ask_luz_tras_reinicio": "Después de reiniciar el módem, ¿la luz de internet quedó normal o sigue con falla (roja, naranja o apagada)?",
    "ask_contrasena": "¿Al conectarse al Wi-Fi le aparece algún mensaje de contraseña incorrecta?",
}
GUIDE_TEXT = {
    "guide_reinicio": "reiniciar el módem: desconectarlo de la corriente, esperar 30 segundos, volver a conectarlo y esperar unos 2 minutos a que las luces se estabilicen",
    "guide_release_renew": "pedir de nuevo una dirección al router: tecla Windows+R, escribir cmd y Enter; escribir ipconfig /release y Enter; luego ipconfig /renew y Enter (sueltan la dirección actual y piden una nueva)",
    "guide_cambiar_dns": "cambiar el DNS del equipo a uno público (por ejemplo 8.8.8.8 y 1.1.1.1) en la configuración del adaptador de red",
    "guide_acercarse_o_5ghz": "acercarse al router o conectarse a la red de 5 GHz si existe, y probar de nuevo",
    "guide_reingresar_clave": "olvidar la red Wi-Fi en el equipo y volver a conectarse escribiendo la clave con cuidado (mayúsculas y minúsculas)",
}
VERIFY_Q = "Cuando termine, ¿ya tiene internet?"
OFFER_Q = "¿Quiere que registre el caso para que un técnico lo revise?"
_TECHNICAL_ACTIONS = re.compile(r"release|renew|dns|ipconfig", re.I)


@dataclass
class Decision:
    kind: str  # ask | guide | explain | offer_escalation | escalate | none
    action: str | None = None  # trace action id
    reason: str = ""
    autonomy: str | None = None
    target: str | None = None  # hypothesis key (explain)
    fact: str | None = None
    marks: list[str] = field(default_factory=list)  # extra action ids considered done after this one
    may_resolve: bool = False


def _hyp_key(rs_hyps: dict, short: str) -> str | None:
    if short in rs_hyps:
        return short
    return next((k for k in rs_hyps if k.split("_", 1)[0] == short), None)


def _restart_known(facts: dict[str, Any]) -> bool:
    return facts.get("luz_tras_reinicio") is not None or bool(facts.get("reinicio_hecho"))


def _requires_ok(spec: dict, facts: dict[str, Any]) -> bool:
    return all(facts.get(k) == v for k, v in (spec.get("requires") or {}).items())


def _compatible_hyps(rs: Any, facts: dict[str, Any]) -> set[str] | None:
    """Hypotheses sharing a fact with any rule that matches a known fact; None when no facts are known."""
    known = {k for k in facts if k in getattr(rs, "facts", {})}
    if not known:
        return None
    out: set[str] = set()
    for r in rs.rules:
        when = r.get("when") if isinstance(r, dict) else getattr(r, "when", None)
        then = r.get("then") if isinstance(r, dict) else getattr(r, "then", None)
        if any(k in known for k in (when or {})):
            out.update(then or {})
    return out or None


def select_action(
    rs: Any,
    hyps: list[dict[str, Any]],
    thresholds: dict[str, float],
    done: set[str],
    facts: dict[str, Any],
    audience: str | None,
    autonomy_policy: dict[str, str],
    verified: bool = False,
) -> Decision:
    """Priority intents are handled by the caller. Here: (2) next plan step of an accepted hypothesis,
    (3) best gain/cost ask or guided step, (4) offer escalation, else none (free diagnostic answer)."""
    labels = {k: v.get("label", k) for k, v in rs.hypotheses.items()}
    own = autonomy_policy.get("ticket_create_own_initiative", "con_confirmacion")
    accepted = sorted((h for h in hyps if h["status"] == "aceptada"), key=lambda h: -h["cf"])
    for h in accepted:
        steps = rs.plans.get(h["key"]) or []
        for i, step in enumerate(steps):
            if isinstance(step, dict):
                verb, arg = next(iter(step.items()))
                if verb != "explain":
                    continue
                target = _hyp_key(rs.hypotheses, str(arg)) or h["key"]
                sid = f"explain:{target}"
                if sid in done:
                    continue
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                marks = ["offer_escalation"] if nxt in ("escalate_ticket", "escalate_ticket_if_unresolved") else []
                return Decision("explain", sid, f"se aceptó «{labels[target]}» (certeza {cf_word(h['cf'])})",
                                "autonomo", target, marks=marks)  # fmt: skip
            if step == "verify" or step in done:
                continue
            if step in ("escalate_ticket", "escalate_ticket_if_unresolved"):
                if verified or "offer_escalation" in done or "escalate_ticket" in done:
                    continue
                kind = "offer_escalation" if own == "con_confirmacion" else "escalate"
                return Decision(kind, "offer_escalation" if kind == "offer_escalation" else "escalate_ticket",
                                f"plan de «{labels[h['key']]}»: escalar a un técnico", own)  # fmt: skip
            if step in _REDUNDANT_AFTER_RESTART and _restart_known(facts):
                continue
            spec = rs.actions.get(step) or {}
            if spec and not _requires_ok(spec, facts):
                continue
            if step.startswith("guide_") or spec.get("kind") == "guided_step":
                return Decision("guide", step, f"plan de «{labels[h['key']]}»: paso guiado",
                                spec.get("autonomy", "autonomo"), h["key"], may_resolve=bool(spec.get("may_resolve", True)))  # fmt: skip
            if spec.get("kind") == "ask" and spec.get("fact") not in facts:
                return Decision(
                    "ask",
                    step,
                    f"plan de «{labels[h['key']]}»",
                    spec.get("autonomy", "autonomo"),
                    fact=spec.get("fact"),
                )
    # (3) utility
    lo, hi = thresholds.get("explore_min", 0.4), thresholds.get("accept", 0.8)
    explore = {h["key"]: h["cf"] for h in hyps if lo <= h["cf"] < hi and h["status"] == "viva"}

    compat = _compatible_hyps(rs, facts)
    alive = {h["key"] for h in hyps if h["cf"] > 0 and h["status"] != "descartada"}

    def relevance(h: dict) -> float:
        if explore:
            return explore.get(h["key"], 0.0)
        if h["status"] == "descartada":
            return 0.0
        if abs(h["cf"]) > 0.05:
            return abs(h["cf"])
        if compat is not None:  # symptoms known: only hypotheses linked to them stay relevant
            return 0.5 if h["key"] in compat else 0.05
        return 0.5

    rel = {h["key"]: relevance(h) for h in hyps} or {k: 0.5 for k in rs.hypotheses}
    gated = {a.get("verify_with"): aid for aid, a in rs.actions.items() if a.get("verify_with")}
    best: tuple[float, str] | None = None
    for aid, a in rs.actions.items():
        if aid in done or a.get("kind") not in ("ask", "guided_step"):
            continue
        if not _requires_ok(a, facts) or (aid in _REDUNDANT_AFTER_RESTART and _restart_known(facts)):
            continue
        if a.get("kind") == "ask" and a.get("fact") in facts:
            continue
        if a.get("sets") and facts.get(a["sets"]) is not None:
            continue
        if aid in gated and gated[aid] not in done and not facts.get("reinicio_hecho"):
            continue
        if explore and not any(hk in alive and g > 0 for hk, g in (a.get("gain") or {}).items()):
            continue  # alive hypotheses exist: never ask what does not discriminate among them
        cost = float(a.get("cost") or 1)
        if audience == "basico" and _TECHNICAL_ACTIONS.search(aid):
            cost *= 2
        score = sum(g * rel.get(hk, 0.0) for hk, g in (a.get("gain") or {}).items()) / cost
        if score > 0 and (best is None or score > best[0]):
            best = (score, aid)
    if best:
        aid = best[1]
        a = rs.actions[aid]
        gain_h = [labels[k] for k, g in (a.get("gain") or {}).items() if g * rel.get(k, 0) > 0]
        reason = (
            "ayuda a distinguir entre " + " y ".join(f"«{x}»" for x in gain_h)
            if len(gain_h) > 1
            else f"ayuda a confirmar o descartar «{gain_h[0]}»"
            if gain_h
            else "es la acción más útil con lo que se sabe"
        )
        if a["kind"] == "ask":
            return Decision("ask", aid, reason, a.get("autonomy", "autonomo"), fact=a.get("fact"))
        return Decision("guide", aid, reason, a.get("autonomy", "autonomo"), may_resolve=bool(a.get("may_resolve")))
    # (4) nothing useful left
    if any(h["cf"] > 0 for h in hyps) and "offer_escalation" not in done and not verified:
        return Decision("offer_escalation", "offer_escalation", "no quedan preguntas ni pasos útiles", own)
    return Decision("none", None, "sin acción específica: respuesta de diagnóstico libre")


# ------------------------------------------------------------------ per-turn instructions (the LLM only phrases)
def _head(level: str) -> str:
    return f"INSTRUCCIÓN (no la menciones): {policy._WHO.get(level, policy._BALANCED)} {policy._NATURAL}"


def _tail(trato: str) -> str:
    return " " + policy._TRATO.get(trato, policy._TRATO["usted"])


def decision_instruction(
    d: Decision, level: str, voice: bool, trato: str, explanation: str = "", checklist_title: str | None = None
) -> str:
    h = _head(level)
    if d.kind == "guide" and checklist_title:  # the steps are shown as a checklist component: never repeat them
        body = (
            f"Invite a la persona, en UNA o DOS frases, a probar «{checklist_title}» (lo hace ELLA, no usted) y dígale que en pantalla tiene los pasos: que marque "
            "«Ya lo hice» en cada uno al terminarlo. NO enumere ni repita los pasos. No ofrezca registrar el caso. "
            "No especule sobre causas técnicas (nada de WAN, sesión, PPPoE, sincronización): solo lo que la persona dijo."
        )
    elif d.kind == "ask":
        q = ASK_TEXT.get(d.action or "", f"¿Me confirma: {(d.fact or '').replace('_', ' ')}?")
        body = (
            "Puede empezar con una frase breve que muestre que entendió lo que dijo. "
            f"Luego pregunte SOLO esto: «{q}». No dé pasos ni soluciones, no haga otras preguntas y no ofrezca registrar el caso. Máximo 2 frases."
        )
    elif d.kind == "guide":
        g = GUIDE_TEXT.get(d.action or "", (d.action or "").removeprefix("guide_").replace("_", " "))
        fmt = "Texto plano, frases cortas, sin listas." if voice else "Use una lista numerada corta (máximo 4 pasos)."
        body = f"Guíe SOLO este paso, con calma y en orden: {g}. {fmt} Al final pregunte SOLO: «{VERIFY_Q}». No ofrezca registrar el caso."
    elif d.kind == "explain":
        offer = f" Luego pregunte SOLO: «{OFFER_Q}»" if "offer_escalation" in d.marks else ""
        body = f"Explique en 2 o 3 frases sencillas la causa más probable, sin inventar nada: {explanation}{offer} Sin listas."
    elif d.kind == "offer_escalation":
        body = f"Diga en 1 o 2 frases que con lo revisado no se resolvió y que lo mejor es que lo revise un técnico. Pregunte SOLO: «{OFFER_Q}» Sin listas."
    else:
        return ""
    return f"{h} {body}" + _tail(trato)


def resolved_instruction(level: str, trato: str, step: str | None) -> str:
    what = f" (lo que funcionó: {GUIDE_TEXT.get(step, step).split(':')[0]})" if step else ""
    return (
        f"{_head(level)} La persona confirma que ya funciona{what}. Alégrese en 1 o 2 frases y avísele que más tarde le escribirá "
        "para confirmar que todo siga bien. Sin listas, sin preguntas y sin ofrecer registrar el caso." + _tail(trato)
    )


def consulta_instruction(level: str, trato: str) -> str:
    return (
        f"{_head(level)} Responda la consulta usando SOLO las fuentes del contexto, con pasos concretos y citas [n]; "
        "si las fuentes no alcanzan, dígalo; no haga preguntas de diagnóstico y no ofrezca registrar el caso." + _tail(trato)
    )


def phrase_instruction(text: str, level: str, trato: str, max_sentences: int = 3) -> str:
    """The LLM only smooths a deterministic text."""
    return (
        f"{_head(level)} Comunique esto con sus palabras, en máximo {max_sentences} frases, sin agregar datos, pasos ni preguntas nuevas: «{text}» Sin listas."
        + _tail(trato)
    )


# ------------------------------------------------------------------ why (spec §3.6)
def why_text(text: str, last_ask: dict | None, explanation: dict | None) -> str:
    """Deterministic explanation: the last question's reason when asked about it, else the top hypothesis."""
    asks_question = re.search(r"pregunt", text, re.I)
    if asks_question and last_ask and last_ask.get("reason"):
        return f"Se lo pregunté porque {last_ask['reason']}."
    if explanation and explanation.get("text"):
        return explanation["text"]
    if last_ask and last_ask.get("reason"):
        return f"Se lo pregunté porque {last_ask['reason']}."
    return "Todavía no tengo suficiente información para sacar una conclusión; por eso le hago preguntas cortas para descartar causas."


# ------------------------------------------------------------------ A1 (ticket follow-up) and K4 templates
STATUS_ES = {
    "open": "abierto, en espera de asignación",
    "assigned": "asignado a un técnico",
    "in_progress": "en atención",
    "resolved": "resuelto por el técnico",
    "closed": "cerrado",
}
NEXT_STEP = {
    "open": "apenas haya un técnico disponible se le asigna",
    "assigned": "el técnico lo contactará o lo visitará según lo acordado",
    "in_progress": "el técnico está trabajando en su caso",
    "resolved": "confírmeme si su conexión ya funciona para cerrarlo",
    "closed": "si el problema volvió, puedo registrar un caso nuevo",
}


def humanize_seconds(s: int) -> str:
    s = abs(int(s))
    if s < 3600:
        m = max(1, s // 60)
        return f"{m} minuto{'s' if m != 1 else ''}"
    if s < 86400:
        h = s // 3600
        return f"{h} hora{'s' if h != 1 else ''}"
    d = s // 86400
    return f"{d} día{'s' if d != 1 else ''}"


def _get(o: Any, k: str, default: Any = None) -> Any:
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def tech_phrase(name: str | None, specialties: list[str] | None) -> str:
    if not name:
        return ""
    spec = ", ".join(s.replace("_", " ") for s in (specialties or [])[:2])
    return f"{name}" + (f" (especialista en {spec})" if spec else "")


def followup_text(tl: Any) -> str:
    """A1 template from a TicketTimeline (or dict with the same fields)."""
    number, status = _get(tl, "ticket_number"), _get(tl, "status", "open")
    parts = [
        f"Su caso {number} está {STATUS_ES.get(status, status)}; se registró hace {humanize_seconds(_get(tl, 'age_seconds', 0))}."
    ]
    tech = _get(tl, "technician")
    if tech:
        parts.append(f"Lo atiende {tech_phrase(_get(tech, 'name'), _get(tech, 'specialties'))}.")
    elif status not in ("resolved", "closed"):
        parts.append("Todavía está en cola: no hay un técnico disponible asignado.")
    note = _get(tl, "last_note")
    if note:
        who = _get(note, "technician_name") or "El técnico"
        parts.append(f"Última nota de {who}: «{_get(note, 'text')}».")
    rem = _get(tl, "sla_remaining_seconds")
    if rem is not None and status not in ("resolved", "closed"):
        parts.append(
            f"El plazo de atención vence en {humanize_seconds(rem)}."
            if rem >= 0
            else "El plazo de atención ya venció y el caso se está priorizando."
        )
    parts.append(f"Próximo paso: {NEXT_STEP.get(status, 'le aviso cuando haya novedades')}.")
    return " ".join(parts)


def created_text(
    number: str, tech_name: str | None, specialties: list[str] | None, queued: bool, known: dict[str, Any]
) -> str:
    """K4 template."""
    parts = [f"Listo, registré su caso con el número {number}."]
    if tech_name and not queued:
        parts.append(f"Lo atenderá {tech_phrase(tech_name, specialties)}.")
    else:
        parts.append(
            "Por ahora no hay un técnico disponible que cumpla las condiciones; el caso quedó en cola y se asignará apenas haya uno."
        )
    when = [MODE_LABELS.get(known.get("modalidad") or "", "")]
    if known.get("franja"):
        when.append(SLOT_LABELS.get(known["franja"], ""))
    w = " ".join(x for x in when if x)
    if w:
        parts.append(f"Quedó anotado: {w}.")
    return " ".join(parts)


# ------------------------------------------------------------------ A3 handoff summary
def handoff_summary(
    session_id: str,
    snap: dict,
    facts: list[dict[str, Any]],
    hyps: list[dict[str, Any]],
    labels: dict[str, str],
    sources: list[str],
    known: dict[str, Any],
    render: Any,
) -> dict[str, Any]:
    sint = [f["label"] for f in snap.get("facts", []) if f["key"] != "needs_guidance"]
    pasos = list(snap.get("steps_tried", []))
    tras = next((f for f in facts if f["key"] == "luz_tras_reinicio"), None)
    if tras and pasos:
        pasos = [p + (" (sigue roja)" if tras["value"] == "roja" and "reinici" in p else "") for p in pasos]
    return {
        "sintomas": sint,
        "hipotesis": [
            {"key": h["key"], "label": labels.get(h["key"], h["key"]), "cf": round(h["cf"], 2)}
            for h in hyps
            if h["cf"] > 0
        ][:3],
        "pasos_probados": pasos,
        "evidencia": [
            {"key": f["key"], "value": f["value"], "source": f["source"], "texto": render(f["key"], f["value"])}
            for f in facts
            if f["key"] not in ("zona", "modalidad", "franja")
        ],
        "fuentes": list(dict.fromkeys(sources))[:5],
        "preferencias": {k: known.get(k) for k in ("zona", "modalidad", "franja") if known.get(k)},
        "conversacion": f"/conversacion/{session_id}",
        "generado": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ generative UI (pure)
# Chip answers that are not catalog facts: the backend turns them into plain text for the normal pipeline.
PSEUDO_ANSWERS = {
    ("verificacion", True): "Sí, ya funciona",
    ("verificacion", False): "No, sigue sin funcionar",
    ("registrar_caso", True): "Sí, registre el caso",
    ("registrar_caso", False): "No, por ahora no",
}
VERIFY_SHORT = "¿Ya tiene internet?"
_STEP_DONE = re.compile(
    r"^\W*(ya\s+(lo\s+)?(hice|hecho|termin\w+|desconect\w+|conect\w+|esper\w+)|listo|hecho|ya\s+est[aá]|siguiente|ya)\b",
    re.I,
)
UI_HINTS = {
    "quick_replies": "Debajo de su mensaje aparecerán botones con las opciones de respuesta: haga la pregunta, pero NO enumere las opciones.",
    "checklist": "En pantalla aparecerá la lista de pasos con botones: NO la repita.",
    "case_card": "En pantalla aparecerá una tarjeta con el estado del caso: no repita sus datos.",
    "hypothesis_bars": "En pantalla aparecerán barras con la certeza de cada causa y el camino del diagnóstico: no repita números ni porcentajes.",
    "timeline": "En pantalla aparecerá la línea de tiempo del caso: no repita cada evento.",
    "topology": "En pantalla aparecerá un diagrama de su red con el tramo que falla: no lo describa nodo por nodo.",
    "report_card": "En pantalla aparecerá un botón para descargar el informe del caso: no dé direcciones web.",
    "image_annotated": "En pantalla aparece la foto con las luces marcadas y una pregunta para confirmar: mencione en media frase lo que se ve en la foto.",
}


def step_done_signal(text: str) -> bool:
    """'ya lo hice', 'listo'… (voice answer to the current checklist step)."""
    t = text.strip()
    return len(t) <= 60 and bool(_STEP_DONE.search(t)) and not works_signal(t)


def ui_hint(components: list[dict]) -> str:
    """Tells the LLM what the screen will show, so it introduces the component instead of repeating it."""
    hints = list(dict.fromkeys(UI_HINTS[c["component"]] for c in components if c.get("component") in UI_HINTS))
    return ("PANTALLA (no la menciones): " + " ".join(hints)) if hints else ""
