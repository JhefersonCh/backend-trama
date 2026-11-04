"""Plain-Spanish rendering of rules, facts and certainty (no voseo). Pure."""

import re
from typing import Any

from .rules import Ruleset

_NUM = re.compile(r"^\s*(<=|>=|==|<|>)\s*(-?\d+(?:\.\d+)?)\s*$")
_CMP = {"<": "menor que", "<=": "menor o igual que", ">": "mayor que", ">=": "mayor o igual que", "==": "igual a"}

# fact key -> (phrase when True, phrase when False) for bools; or template with {v} for the rest.
_BOOL = {
    "otros_equipos_navegan": ("otros equipos sí navegan", "ningún otro equipo navega"),
    "reinicio_hecho": ("ya se reinició el equipo", "todavía no se ha reiniciado el equipo"),
    "ip_apipa": (
        "el equipo tiene una dirección IP automática inválida (169.254.x.x)",
        "el equipo tiene una dirección IP válida",
    ),
    "navega_por_ip": ("se puede navegar usando direcciones IP", "no se puede navegar ni siquiera por dirección IP"),
    "resuelve_nombres": ("se resuelven los nombres de páginas", "no se resuelven los nombres de páginas"),
    "funciona_por_cable": ("funciona conectado por cable", "no funciona conectado por cable"),
    "clave_cambiada": ("la clave del Wi-Fi fue cambiada", "la clave del Wi-Fi no fue cambiada"),
    "error_contrasena": ("aparece un error de contraseña", "no aparece error de contraseña"),
    "caida_reportada_zona": ("hay una caída reportada en la zona", "no hay caídas reportadas en la zona"),
}
_TEMPLATE = {
    "luz_modem": "la luz del módem está {v}",
    "luz_tras_reinicio": "la luz del módem sigue {v} tras reiniciar",
    "senal_wifi_pct": "la señal Wi-Fi es {v}",
    "zona": "la zona es {v}",
    "modalidad": "la atención es {v}",
    "franja": "la franja preferida es {v}",
}


def _humanize(key: str) -> str:
    return key.replace("_", " ")


def _fmt(value: Any, key: str) -> str:
    if isinstance(value, list):
        return " o ".join(str(x) for x in value)
    if isinstance(value, str) and (m := _NUM.match(value)):
        return f"{_CMP[m.group(1)]} {m.group(2)}{' %' if key.endswith('_pct') else ''}"
    if key.endswith("_pct") and isinstance(value, int | float):
        return f"{value} %"
    return str(value)


def render_fact(key: str, value: Any) -> str:
    """Natural phrase for a fact value or a rule condition (scalar, list or "<50")."""
    if key in _BOOL and isinstance(value, bool):
        return _BOOL[key][0 if value else 1]
    if key in _TEMPLATE:
        return _TEMPLATE[key].format(v=_fmt(value, key))
    return f"{_humanize(key)}: {_fmt(value, key)}"


def render_when(when: dict[str, Any]) -> str:
    return " y ".join(render_fact(k, v) for k, v in when.items())


def cf_word(cf: float) -> str:
    a = abs(cf)
    return "alta" if a >= 0.8 else "media" if a >= 0.5 else "baja"


def build_explanation(
    ruleset: Ruleset,
    target: str,
    cf: float,
    status: str,
    fired: list[str],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    label = ruleset.hypotheses[target]["label"]
    by_id = {r.id: r for r in ruleset.rules}
    rules = []
    for rid in fired:
        r = by_id.get(rid)
        if r and target in r.then:
            rules.append(
                {
                    "id": rid,
                    "when": render_when(r.when),
                    "text": render_when(r.when),
                    "effect": "a_favor" if r.then[target] > 0 else "en_contra",
                    "cf": r.then[target],
                }
            )
    used = {k for rid in fired if (r := by_id.get(rid)) and target in r.then for k in r.when}
    evid = [{"key": f["key"], "text": render_fact(f["key"], f["value"]), "source": f.get("source")}
            for f in facts if f["key"] in used]  # fmt: skip
    conf = cf_word(cf)
    if not rules:
        text = f"Todavía no tengo información que apoye o descarte «{label}»."
    else:
        pro = [r["when"] for r in rules if r["effect"] == "a_favor"]
        con = [r["when"] for r in rules if r["effect"] == "en_contra"]
        parts = []
        if pro:
            parts.append("porque " + " y ".join(pro))
        if con:
            parts.append(("pero " if pro else "porque ") + " y ".join(con) + (" va en contra" if pro else ""))
        verdict = {"aceptada": "Creo", "viva": "Sospecho", "descartada": "Descarto"}.get(status, "Considero")
        subject = f"que es «{label}»" if status != "descartada" else f"«{label}»"
        text = f"{verdict} {subject} con confianza {conf}, " + ", ".join(parts) + "."
    return {"target": target, "label": label, "cf": cf, "confidence": conf, "status": status, "rules": rules,
            "facts": evid, "text": text}  # fmt: skip
