"""Forward-chaining inference over certainty factors. Pure: no I/O."""

from dataclasses import dataclass, field
from typing import Any

from .cf import combine, weighted
from .rules import Ruleset, matches

DEFAULT_THRESHOLDS = {"accept": 0.8, "explore_min": 0.4, "reject": -0.5}

Facts = dict[str, tuple[Any, float]]  # key -> (value, certainty)


@dataclass
class ForwardResult:
    hypotheses: dict[str, float]
    fired: list[str]
    contributions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # hyp -> [{rule, cf}]


def forward_chain(facts: Facts, ruleset: Ruleset) -> ForwardResult:
    """Recomputes every hypothesis from scratch. A rule fires when ALL its conditions hold on known facts;
    its CF is scaled by the weakest certainty among the facts it used."""
    cfs = {h: 0.0 for h in ruleset.hypotheses}
    contributions: dict[str, list[dict[str, Any]]] = {h: [] for h in ruleset.hypotheses}
    fired: list[str] = []
    for rule in ruleset.rules:
        if not all(k in facts and matches(cond, facts[k][0]) for k, cond in rule.when.items()):
            continue
        certainty = min(facts[k][1] for k in rule.when)
        fired.append(rule.id)
        for hyp, rcf in rule.then.items():
            applied = weighted(rcf, certainty)
            cfs[hyp] = combine(cfs[hyp], applied)
            contributions[hyp].append({"rule": rule.id, "cf": round(applied, 6)})
    return ForwardResult(
        {h: round(v, 6) for h, v in cfs.items()},
        fired,
        {h: c for h, c in contributions.items() if c},
    )


def classify_status(cf: float, thresholds: dict[str, float] | None = None) -> str:
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if cf >= t["accept"]:
        return "aceptada"
    if cf <= t["reject"]:
        return "descartada"
    return "viva"
