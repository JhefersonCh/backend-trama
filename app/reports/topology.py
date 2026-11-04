# ruff: noqa: E501
"""Server-rendered home-network topology (SVG): ISP -> modem/ONT -> router -> this device / other devices.

Node colour = what the case facts say about that segment (ok / warn / bad / unknown). Pure function of the facts.
"""

from html import escape
from typing import Any

OK, WARN, BAD, UNK = "ok", "warn", "bad", "unk"
COLORS = {  # (stroke, soft fill) - print-safe, same family as the app's light theme
    OK: ("#1f7a45", "#e6f3ec"),
    WARN: ("#b04f08", "#fbeadb"),
    BAD: ("#b3261e", "#f9e1df"),
    UNK: ("#8a8478", "#f1eee7"),
}
LEGEND = {OK: "normal", WARN: "con alerta", BAD: "con falla", UNK: "sin datos"}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def assess(facts: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """facts {key: value} -> {node: (status, caption)}."""
    f = facts
    luz, luz2 = f.get("luz_modem"), f.get("luz_tras_reinicio")
    others = f.get("otros_equipos_navegan")

    if f.get("caida_reportada_zona") is True:
        isp = (BAD, "caída reportada en la zona")
    elif others is False and luz in ("roja", "naranja", "apagada"):
        isp = (BAD, "falla de la línea")
    elif others is True:
        isp = (OK, "llega servicio")
    elif f.get("caida_reportada_zona") is False:
        isp = (OK, "sin caídas reportadas")
    else:
        isp = (UNK, "sin datos")

    if luz2 == "roja":
        modem = (BAD, "luz roja tras reiniciar")
    elif luz in ("roja", "apagada"):
        modem = (BAD, f"luz {luz}")
    elif luz in ("naranja", "parpadea"):
        modem = (WARN, f"luz {luz}")
    elif luz == "verde" or luz2 == "normal":
        modem = (OK, "luz verde" if luz == "verde" else "luz normal")
    else:
        modem = (UNK, "sin datos")

    pct = _num(f.get("senal_wifi_pct"))
    if f.get("error_contrasena") is True:
        router = (WARN, "error de clave Wi-Fi")
    elif pct is not None and pct < 50:
        router = (WARN, f"señal Wi-Fi {pct:g} %")
    elif others is True or f.get("funciona_por_cable") is True:
        router = (OK, f"señal Wi-Fi {pct:g} %" if pct is not None else "responde")
    elif others is False and modem[0] == BAD:
        router = (UNK, "sin internet")
    else:
        router = (UNK, "sin datos")

    if f.get("ip_apipa") is True:
        dev = (BAD, "IP 169.254.x.x")
    elif f.get("navega_por_ip") is False:
        dev = (BAD, "no navega ni por IP")
    elif f.get("resuelve_nombres") is False:
        dev = (WARN, "falla el DNS")
    elif f.get("navega_por_ip") is True and f.get("resuelve_nombres") is True:
        dev = (OK, "navega con normalidad")
    elif f.get("funciona_por_cable") is True:
        dev = (WARN, "solo falla por Wi-Fi")
    else:
        dev = (UNK, "sin datos")

    oth = {True: (OK, "sí navegan"), False: (BAD, "ninguno navega")}.get(others, (UNK, "sin datos"))
    return {"isp": isp, "modem": modem, "router": router, "device": dev, "others": oth}


def _node(x: int, y: int, w: int, h: int, title: str, sub: str, caption: str, status: str) -> str:
    stroke, fill = COLORS[status]
    dash = ' stroke-dasharray="5 4"' if status == UNK else ""
    return (
        f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="2"{dash}/>'
        f'<circle cx="{x + 16}" cy="{y + 18}" r="6" fill="{stroke}"/>'
        f'<text x="{x + 30}" y="{y + 22}" font-family="Helvetica,Arial,sans-serif" font-size="13" font-weight="700" fill="#1d1a15">{escape(title)}</text>'
        f'<text x="{x + 16}" y="{y + 42}" font-family="Helvetica,Arial,sans-serif" font-size="11" fill="#6b655a">{escape(sub)}</text>'
        f'<text x="{x + 16}" y="{y + h - 12}" font-family="DejaVu Sans Mono,monospace" font-size="10.5" font-weight="700" fill="{stroke}">{escape(caption)}</text></g>'
    )


def render_topology(facts: dict[str, Any]) -> str:
    a = assess(facts)
    W, H, NW, NH = 760, 250, 150, 78
    xs = [10, 205, 400, 600]
    y_mid = 86
    nodes = [
        ("isp", xs[0], y_mid, "Internet", "proveedor / línea"),
        ("modem", xs[1], y_mid, "Módem / ONT", "luces de estado"),
        ("router", xs[2], y_mid, "Router Wi-Fi", "red de la casa"),
    ]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
        'aria-label="Topología de la red del hogar con el estado de cada tramo">',
    ]
    # links first (under the nodes); colour = worst of the two ends it connects
    rank = {OK: 0, UNK: 1, WARN: 2, BAD: 3}

    def link(x1: int, y1: int, x2: int, y2: int, s1: str, s2: str) -> str:
        s = max(s1, s2, key=lambda s: rank[s])
        d = ' stroke-dasharray="6 5"' if s == UNK else ""
        return f'<path d="M{x1} {y1} L{x2} {y2}" stroke="{COLORS[s][0]}" stroke-width="3" fill="none"{d}/>'

    cy = y_mid + NH // 2
    out.append(link(xs[0] + NW, cy, xs[1], cy, a["isp"][0], a["modem"][0]))
    out.append(link(xs[1] + NW, cy, xs[2], cy, a["modem"][0], a["router"][0]))
    out.append(link(xs[2] + NW, cy, xs[3], 30 + NH // 2, a["router"][0], a["device"][0]))
    out.append(link(xs[2] + NW, cy, xs[3], 150 + NH // 2, a["router"][0], a["others"][0]))
    for key, x, y, t, s in nodes:
        out.append(_node(x, y, NW, NH, t, s, a[key][1], a[key][0]))
    out.append(_node(xs[3], 30, NW, NH, "Este equipo", "el que tiene el problema", a["device"][1], a["device"][0]))
    out.append(_node(xs[3], 150, NW, NH, "Otros equipos", "celulares, TV, etc.", a["others"][1], a["others"][0]))
    lx = 10
    for st in (OK, WARN, BAD, UNK):
        out.append(f'<rect x="{lx}" y="{H - 22}" width="14" height="10" rx="3" fill="{COLORS[st][1]}" '
                   f'stroke="{COLORS[st][0]}" stroke-width="2"/>'
                   f'<text x="{lx + 20}" y="{H - 13}" font-family="Helvetica,Arial,sans-serif" font-size="10.5" fill="#6b655a">{LEGEND[st]}</text>')  # fmt: skip
        lx += 118
    out.append("</svg>")
    return "".join(out)
