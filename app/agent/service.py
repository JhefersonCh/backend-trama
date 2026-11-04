# ruff: noqa: E501
"""Agent core use cases. PART 2 calls: upsert_fact(), recompute(), explain(), case_view()."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.config.errors import DomainValidationError, NotFoundError

from .engine import DEFAULT_THRESHOLDS, classify_status, forward_chain
from .explain import build_explanation, render_fact
from .ports import AgentRepo
from .rules import Ruleset, RulesetError, parse_ruleset, validate_ruleset, value_ok

FACT_SOURCES = (
    "user_reported",
    "browser_measured",
    "local_agent",
    "router",
    "public_api",
    "photo",
    "technician",
    "system",
)
CASE_DEFAULTS = {"state": "nuevo", "goal": "restablecer_conectividad", "subgoals": [], "verified": False}


@dataclass
class RecomputeResult:
    hypotheses: list[dict[str, Any]]  # [{key, cf, status}] sorted by cf desc
    fired: list[str]
    contributions: dict[str, list[dict[str, Any]]]


class AgentService:
    def __init__(self, repo: AgentRepo) -> None:
        self.repo = repo

    # --- rules
    async def load_ruleset(self) -> Ruleset:
        row = await self.repo.active_rule_set()
        if row is None:
            raise NotFoundError("no active rule set")
        return parse_ruleset(row["content_yaml"], row["version"])

    async def thresholds(self) -> dict[str, float]:
        v = await self.repo.get_setting("cf_thresholds")
        return {**DEFAULT_THRESHOLDS, **(v if isinstance(v, dict) else {})}

    def validate(self, content_yaml: str) -> dict[str, Any]:
        errors = validate_ruleset(content_yaml)
        return {"ok": not errors, "errors": errors}

    async def create_rule_set(self, content_yaml: str) -> dict[str, Any]:
        if not content_yaml.strip():
            raise DomainValidationError("content_yaml is empty")
        row = await self.repo.create_rule_set(content_yaml)  # drafts may be invalid; activation checks
        return {"version": row["version"], "is_active": row["is_active"], **self.validate(content_yaml)}

    async def activate_rule_set(self, version: int) -> dict[str, Any]:
        row = await self.repo.get_rule_set(version)
        if row is None:
            raise NotFoundError(f"rule set version {version} not found")
        try:
            parse_ruleset(row["content_yaml"])
        except RulesetError as e:
            raise DomainValidationError(f"rule set v{version} is invalid and cannot be activated: {e}") from e
        await self.repo.activate_rule_set(version)
        return {"version": version, "is_active": True}

    # --- facts
    async def _require_session(self, sid: UUID) -> None:
        if not await self.repo.session_exists(sid):
            raise NotFoundError("session not found")

    async def upsert_fact(
        self, sid: UUID, key: str, value: Any, source: str, certainty: float = 1.0,
        evidence_ref: UUID | None = None, turn_id: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Validates against the active catalog (closed domain), supersedes the previous fact of the same key."""
        if source not in FACT_SOURCES:
            raise DomainValidationError(f"source must be one of {list(FACT_SOURCES)}")
        if not 0 <= certainty <= 1:
            raise DomainValidationError("certainty must be between 0 and 1")
        await self._require_session(sid)
        rs = await self.load_ruleset()
        spec = rs.facts.get(key)
        if spec is None:
            raise DomainValidationError(f"unknown fact key: {key}")
        if not value_ok(spec, value):
            raise DomainValidationError(f"value {value!r} is outside the domain of fact {key}")
        await self.repo.ensure_case(sid)
        return await self.repo.upsert_fact(sid, key, value, source, certainty, evidence_ref, turn_id)

    # --- inference
    async def recompute(self, sid: UUID, turn_id: str | None = None) -> RecomputeResult:
        """Percibir is done; this is CREER: active facts + active rules -> hypotheses (persisted) + trace."""
        await self._require_session(sid)
        rs = await self.load_ruleset()
        th = await self.thresholds()
        facts = await self.repo.active_facts(sid)
        res = forward_chain({f["key"]: (f["value"], f["certainty"]) for f in facts}, rs)
        hyps = sorted(
            ({"key": k, "cf": cf, "status": classify_status(cf, th)} for k, cf in res.hypotheses.items()),
            key=lambda h: (-h["cf"], h["key"]),
        )
        await self.repo.ensure_case(sid)
        await self.repo.replace_hypotheses(sid, hyps)
        used = {k for r in rs.rules if r.id in res.fired for k in r.when}
        await self.repo.append_trace(
            sid, "creer",
            facts_used=[{"key": f["key"], "value": f["value"], "source": f["source"]} for f in facts if f["key"] in used],
            rules_fired=res.fired, hypotheses=hyps, turn_id=turn_id,
            reason=f"{len(res.fired)} regla(s) disparada(s) con la versión {rs.version} de las reglas",
        )  # fmt: skip
        return RecomputeResult(hyps, res.fired, res.contributions)

    # --- case (PART 2: used by the conversation turn)
    async def actions_taken(self, sid: UUID) -> list[str]:
        return await self.repo.actions_taken(sid)

    async def set_case(
        self, sid: UUID, *, state: str | None = None, verified: bool | None = None, ticket_number: str | None = None
    ) -> dict[str, Any]:
        await self.repo.ensure_case(sid)
        await self.repo.update_case(sid, state=state, verified=verified, ticket_number=ticket_number)
        return await self.repo.get_case(sid) or {}

    async def get_case(self, sid: UUID) -> dict[str, Any] | None:
        return await self.repo.get_case(sid)

    async def hypotheses(self, sid: UUID) -> list[dict[str, Any]]:
        return await self.repo.get_hypotheses(sid)

    async def active_facts(self, sid: UUID) -> list[dict[str, Any]]:
        return await self.repo.active_facts(sid)

    async def trace(self, sid: UUID, phase: str, **kw: Any) -> None:
        kw.setdefault("facts_used", [])
        kw.setdefault("rules_fired", [])
        kw.setdefault("hypotheses", [])
        await self.repo.append_trace(sid, phase, **kw)

    # --- views
    async def case_view(self, sid: UUID) -> dict[str, Any]:
        await self._require_session(sid)
        case = await self.repo.get_case(sid) or {}
        facts = await self.repo.active_facts(sid)
        hyps = await self.repo.get_hypotheses(sid)
        try:
            labels = {k: v["label"] for k, v in (await self.load_ruleset()).hypotheses.items()}
        except NotFoundError:
            labels = {}
        ticket = (
            {"id": case["ticket_id"], "ticket_number": case.get("ticket_number"), "status": case.get("ticket_status")}
            if case.get("ticket_id") else None
        )  # fmt: skip
        return {
            **{k: case.get(k, d) for k, d in CASE_DEFAULTS.items()},
            "session_id": sid,
            "ticket": ticket,
            "facts": [{**f, "text": render_fact(f["key"], f["value"])} for f in facts],
            "hypotheses": [{**h, "label": labels.get(h["key"], h["key"])} for h in hyps],
            "trace": await self.repo.list_trace(sid, 50),
        }

    async def explain(self, sid: UUID, target: str | None = None) -> dict[str, Any]:
        await self._require_session(sid)
        rs = await self.load_ruleset()
        hyps = {h["key"]: h for h in await self.repo.get_hypotheses(sid)}
        if target is None:
            if not hyps:
                raise NotFoundError("no hypotheses yet for this case")
            target = max(hyps.values(), key=lambda h: h["cf"])["key"]
        if target not in rs.hypotheses:
            raise NotFoundError(f"unknown hypothesis: {target}")
        trace = await self.repo.latest_trace(sid, "creer")
        h = hyps.get(target, {"cf": 0.0, "status": "viva"})
        out = build_explanation(
            rs, target, h["cf"], h["status"], list(trace["rules_fired"]) if trace else [],
            await self.repo.active_facts(sid),
        )  # fmt: skip
        out["trace_id"] = trace["id"] if trace else None
        return out
