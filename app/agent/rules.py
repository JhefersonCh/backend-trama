"""Rule-set YAML: parsing, validation (structured errors) and condition matching. Pure."""

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

FACT_TYPES = ("enum", "bool", "int", "string")
ACTION_KINDS = ("ask", "guided_step", "client_tool", "tool")
UI_COMPONENTS = ("quick_replies", "checklist", "form", "compare_cards", "gauge")
AUTONOMY_LEVELS = ("autonomo", "con_aviso", "con_confirmacion")
RESERVED_STEPS = ("verify", "escalate_ticket", "escalate_ticket_if_unresolved")
_NUM_COND = re.compile(r"^\s*(<=|>=|==|<|>)\s*(-?\d+(?:\.\d+)?)\s*$")
_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
}


@dataclass(frozen=True)
class Rule:
    id: str
    when: dict[str, Any]
    then: dict[str, float]


@dataclass(frozen=True)
class Ruleset:
    name: str
    facts: dict[str, dict]
    hypotheses: dict[str, dict]
    rules: tuple[Rule, ...]
    actions: dict[str, dict] = field(default_factory=dict)
    plans: dict[str, list] = field(default_factory=dict)
    version: int | None = None


class RulesetError(Exception):
    def __init__(self, errors: list[dict[str, str]]):
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))
        self.errors = errors


def is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def value_ok(spec: dict, v: Any) -> bool:
    t = spec.get("type")
    if t == "enum":
        return isinstance(v, str) and v in spec.get("values", [])
    if t == "bool":
        return isinstance(v, bool)
    if t == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "string":
        return isinstance(v, str)
    return False


def matches(cond: Any, value: Any) -> bool:
    """cond: scalar (equality), list (membership) or "<50"-style numeric comparison."""
    if isinstance(cond, list):
        return value in cond
    if isinstance(cond, str) and (m := _NUM_COND.match(cond)):
        return is_number(value) and _OPS[m.group(1)](value, float(m.group(2)))
    return value == cond and isinstance(value, bool) == isinstance(cond, bool)


def _check(text: str) -> tuple[Ruleset | None, list[dict[str, str]]]:
    errs: list[dict[str, str]] = []

    def err(path: str, msg: str) -> None:
        errs.append({"path": path, "message": msg})

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, [{"path": "", "message": f"YAML inválido: {str(e).splitlines()[0] if str(e) else 'error'}"}]
    if not isinstance(data, dict):
        return None, [{"path": "", "message": "el documento debe ser un mapa YAML"}]

    def section(name: str, required: bool = True) -> dict:
        v = data.get(name)
        if v is None:
            if required:
                err(name, "sección requerida")
            return {}
        if not isinstance(v, dict):
            err(name, "debe ser un mapa")
            return {}
        return v

    facts, hyps = section("facts"), section("hypotheses")
    actions, plans = section("actions", False), section("plans", False)
    rules_raw = data.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        err("rules", "debe ser una lista no vacía")
        rules_raw = []

    for k, spec in facts.items():
        p = f"facts.{k}"
        if not isinstance(spec, dict) or spec.get("type") not in FACT_TYPES:
            err(p, f"type debe ser uno de {list(FACT_TYPES)}")
        elif spec["type"] == "enum" and not (
            isinstance(spec.get("values"), list) and spec["values"] and all(isinstance(x, str) for x in spec["values"])
        ):
            err(p, "enum requiere 'values' (lista de textos)")
    for k, spec in hyps.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("label"), str) or not spec["label"].strip():
            err(f"hypotheses.{k}", "requiere 'label'")

    rules: list[Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(rules_raw):
        p = f"rules[{i}]"
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"]:
            err(p, "cada regla requiere 'id' (texto)")
            continue
        rid = r["id"]
        p = f"rules.{rid}"
        if rid in seen:
            err(p, "id duplicado")
        seen.add(rid)
        when, then = r.get("when"), r.get("then")
        ok = True
        if not isinstance(when, dict) or not when:
            err(f"{p}.when", "debe ser un mapa no vacío")
            ok = False
        else:
            for fk, cond in when.items():
                spec = facts.get(fk)
                if not isinstance(spec, dict):
                    err(f"{p}.when.{fk}", "el hecho no existe en 'facts'")
                    ok = False
                    continue
                if not _cond_ok(spec, cond):
                    err(f"{p}.when.{fk}", f"valor {cond!r} fuera del dominio del hecho")
                    ok = False
        if not isinstance(then, dict) or not then:
            err(f"{p}.then", "debe ser un mapa no vacío")
            ok = False
        else:
            for hk, cf in then.items():
                if hk not in hyps:
                    err(f"{p}.then.{hk}", "la hipótesis no existe en 'hypotheses'")
                    ok = False
                elif not is_number(cf) or not -1 <= cf <= 1:
                    err(f"{p}.then.{hk}", "el CF debe ser un número entre -1 y 1")
                    ok = False
        if ok:
            rules.append(Rule(rid, dict(when), {k: float(v) for k, v in then.items()}))

    _check_actions(actions, facts, hyps, err)
    _check_plans(plans, actions, hyps, err)
    if errs:
        return None, errs
    return Ruleset(
        str(data.get("name") or "diagnostico_red"),
        facts,
        hyps,
        tuple(rules),
        actions,
        plans,
        data.get("version") if isinstance(data.get("version"), int) else None,
    ), []


def _cond_ok(spec: dict, cond: Any) -> bool:
    if isinstance(cond, list):
        return spec.get("type") in ("enum", "string") and bool(cond) and all(value_ok(spec, c) for c in cond)
    if spec.get("type") == "int" and isinstance(cond, str):
        return bool(_NUM_COND.match(cond))
    return value_ok(spec, cond)


def _check_actions(actions: dict, facts: dict, hyps: dict, err: Any) -> None:
    for aid, a in actions.items():
        p = f"actions.{aid}"
        if not isinstance(a, dict) or a.get("kind") not in ACTION_KINDS:
            err(p, f"kind debe ser uno de {list(ACTION_KINDS)}")
            continue
        if a["kind"] == "ask" and a.get("fact") not in facts:
            err(f"{p}.fact", "el hecho preguntado no existe en 'facts'")
        if "sets" in a and a["sets"] not in facts:
            err(f"{p}.sets", "el hecho no existe en 'facts'")
        if "verify_with" in a and a["verify_with"] not in actions:
            err(f"{p}.verify_with", "la acción no existe en 'actions'")
        for fk in a.get("requires") or {}:
            if fk not in facts:
                err(f"{p}.requires.{fk}", "el hecho no existe en 'facts'")
        for hk, g in (a.get("gain") or {}).items():
            if hk not in hyps:
                err(f"{p}.gain.{hk}", "la hipótesis no existe en 'hypotheses'")
            elif not is_number(g) or not 0 <= g <= 1:
                err(f"{p}.gain.{hk}", "la ganancia debe estar entre 0 y 1")
        automatic = a["kind"] in ("tool", "client_tool")  # no effort for the person: may cost 0
        if not is_number(a.get("cost")) or a["cost"] < 0 or (a["cost"] == 0 and not automatic):
            err(f"{p}.cost", "el costo debe ser un número > 0" + (" (o 0 en herramientas)" if automatic else ""))
        if a.get("autonomy") is not None and a["autonomy"] not in AUTONOMY_LEVELS:
            err(f"{p}.autonomy", f"debe ser uno de {list(AUTONOMY_LEVELS)}")
        if a["kind"] == "tool":
            if not isinstance(a.get("tool"), str) or not a["tool"]:
                err(f"{p}.tool", "una acción 'tool' requiere el nombre de la herramienta")
            for arg, fk in (a.get("args_from_facts") or {}).items():
                if fk not in facts:
                    err(f"{p}.args_from_facts.{arg}", "el hecho no existe en 'facts'")
        if "ui" in a:
            _check_ui(p, a, err)


def _check_ui(p: str, a: dict, err: Any) -> None:
    """`ui` declares the chat component of the action (spec UI §1.3)."""
    ui = a["ui"]
    if not isinstance(ui, dict) or ui.get("component") not in UI_COMPONENTS:
        err(f"{p}.ui.component", f"debe ser uno de {list(UI_COMPONENTS)}")
        return
    if ui["component"] == "quick_replies" and a["kind"] != "ask":
        err(f"{p}.ui", "quick_replies solo aplica a acciones 'ask'")
    if ui["component"] == "checklist":
        steps = ui.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, str) and s.strip() for s in steps):
            err(f"{p}.ui.steps", "checklist requiere 'steps' (lista de textos no vacía)")
        elif len(steps) > 12:
            err(f"{p}.ui.steps", "máximo 12 pasos")
    for k in ("question", "title"):
        if k in ui and not (isinstance(ui[k], str) and ui[k].strip()):
            err(f"{p}.ui.{k}", "debe ser un texto no vacío")


def _step_ref(step: Any) -> str | None:
    return step if isinstance(step, str) else None


def _check_plans(plans: dict, actions: dict, hyps: dict, err: Any) -> None:
    graph: dict[str, set[str]] = {}
    for hk, steps in plans.items():
        p = f"plans.{hk}"
        if hk not in hyps:
            err(p, "la hipótesis no existe en 'hypotheses'")
        if not isinstance(steps, list):
            err(p, "debe ser una lista de pasos")
            continue
        graph[hk] = set()
        for s in steps:
            if isinstance(s, dict) and len(s) == 1 and isinstance(next(iter(s.values())), str):
                continue  # {explain: H1}
            ref = _step_ref(s)
            if ref is None:
                err(p, f"paso inválido: {s!r}")
            elif ref in hyps:
                graph[hk].add(ref)  # jump to another hypothesis' plan
            elif not (ref in actions or ref in RESERVED_STEPS or ref.startswith("guide_")):
                err(p, f"paso '{ref}' no existe (ni acción, ni guide_*, ni paso reservado)")
    state: dict[str, int] = {}

    def dfs(n: str) -> bool:
        state[n] = 1
        for m in graph.get(n, ()):
            if state.get(m) == 1 or (state.get(m) is None and dfs(m)):
                return True
        state[n] = 2
        return False

    for n in graph:
        if state.get(n) is None and dfs(n):
            err("plans", "ciclo detectado entre planes")
            break


def validate_ruleset(text: str) -> list[dict[str, str]]:
    """Structured errors [{path, message}]; empty list = valid."""
    return _check(text)[1]


def parse_ruleset(text: str, version: int | None = None) -> Ruleset:
    rs, errs = _check(text)
    if rs is None:
        raise RulesetError(errs)
    return Ruleset(rs.name, rs.facts, rs.hypotheses, rs.rules, rs.actions, rs.plans, version or rs.version)
