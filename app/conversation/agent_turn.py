# ruff: noqa: E501
"""Agent cycle inside a conversation turn: percibir -> creer -> decidir -> actuar -> verificar (spec §3.2).
Code decides (facts, CF, action, lifecycle, ticket + assignment); the LLM only phrases the chosen action."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.agent.explain import render_fact
from app.agent.lifecycle import next_state
from app.config.errors import DomainValidationError, NotFoundError
from app.ui import builders as ub
from app.ui.models import EVENT_ACTIONS

from . import agent_policy as ap
from . import policy
from .ports import CaseTicketPort, NoopProactive, ProactivePort

if TYPE_CHECKING:
    from .service import ConversationService, TurnState

log = logging.getLogger("trama.agent_turn")

PROBLEM_INTENTS = {policy.DIAGNOSTIC, policy.OUTPUT, policy.CLARIFY, policy.GUIDE, policy.TICKET_CREATE}
DEFAULT_ZONES = list(ap.ZONE_LABELS)
DEFAULT_QUESTIONS = list(ap.SLOTS)
DEFAULT_AUTONOMY = {"ticket_create_on_request": "con_aviso", "ticket_create_own_initiative": "con_confirmacion"}
DEFAULT_FOLLOWUP = {"after_ticket": 24, "after_step": 0.25}
CONFIRM_ACTION = "confirm:ticket_create"
PROGRESS = {
    "retrieving": "Estoy revisando la información de su caso…",
    "thinking": "Buscando la mejor forma de ayudarle…",
    "ticket": "Registrando su caso…",
}


@dataclass
class AgentPlan:
    tail: str | None = None  # replaces policy.instruction (None = legacy instruction)
    fixed_text: str | None = None  # deterministic text, no LLM
    fallback_text: str | None = None  # used when the LLM returns nothing
    extra_tail: str = ""  # appended to the legacy instruction
    rag: bool = True
    context: bool = True
    ticket_offer: bool | None = None
    intent: str | None = None
    max_tokens: int | None = None
    confirm: dict | None = None
    case_update: dict | None = None
    snapshot: dict = field(default_factory=dict)
    components: list[dict] = field(default_factory=list)  # ui_component envelopes emitted after the text
    card: dict | None = None  # {"ticket": …} -> case_card at the end of the cycle (state already final)
    bars: bool = False
    report: bool = False

    @property
    def passthrough(self) -> bool:
        return self.tail is None and self.fixed_text is None


class CaseAgent:
    def __init__(self, agent: Any, tickets: CaseTicketPort | None, proactive: ProactivePort | None = None) -> None:
        self.agent = agent
        self.tickets = tickets
        self.proactive: ProactivePort = proactive or NoopProactive()
        self._contacts: dict[str, str] = {}  # session -> contact waiting for the ticket (never persisted elsewhere)
        self._topo: dict[str, tuple[str, int]] = {}  # session -> (topology signature, turn number) last emitted
        self._turn_no: dict[str, int] = {}
        self._device: dict[str, str] = {}  # session -> failing device kind ("celular" | "pc"); not a catalog fact
        self._node_clicks: dict[str, tuple[str, int]] = {}  # session -> (topology node, turn) last answered

    # ------------------------------------------------------------------ helpers
    async def _setting(self, svc: "ConversationService", key: str, default: Any, typ: type) -> Any:
        try:
            v = await svc.settings.get(key, default)
        except Exception:
            return default
        return v if isinstance(v, typ) else default

    async def _trace(self, sid: UUID, ts: "TurnState", phase: str, **kw: Any) -> None:
        try:
            await self.agent.trace(sid, phase, turn_id=ts.turn_id, **kw)
        except Exception as e:  # the trace never breaks a turn
            log.warning("trace %s failed: %s", phase, type(e).__name__)

    async def _decide(
        self, sid: UUID, ts: "TurnState", action: str, reason: str, autonomy: str | None, hyps: list, done_reason: str
    ) -> None:
        snap = [{"key": h["key"], "cf": h["cf"], "status": h["status"]} for h in hyps]
        await self._trace(sid, ts, "decidir", action=action, reason=reason, autonomy=autonomy, hypotheses=snap)
        await self._trace(sid, ts, "actuar", action=action, reason=done_reason, autonomy=autonomy)

    async def _schedule(self, kind: str, **kw: Any) -> None:
        try:
            await self.proactive.schedule(kind, **kw)
        except Exception as e:
            log.warning("could not schedule %s: %s", kind, type(e).__name__)

    # ------------------------------------------------------------------ turn
    async def before(
        self,
        svc: "ConversationService",
        ts: "TurnState",
        text: str,
        contact: str | None,
        intent: str,
        level: str,
        snap: dict,
        user_texts: list[str],
        trato: str,
    ) -> AgentPlan:
        sid = UUID(ts.session_id)
        rs = await self.agent.load_ruleset()
        th = await self.agent.thresholds()
        labels = {k: v.get("label", k) for k, v in rs.hypotheses.items()}
        case = await self.agent.get_case(sid) or {}
        state, verified = case.get("state") or "nuevo", bool(case.get("verified"))
        taken = await self.agent.actions_taken(sid) if case else []
        done, last = set(taken), (taken[-1] if taken else None)
        intent = ts.ui_intent or ap.refine_intent(text, intent)
        ui_turn = ts.ui_event is not None
        zones = [
            z for z in await self._setting(svc, "zones", DEFAULT_ZONES, list) if isinstance(z, str)
        ] or DEFAULT_ZONES
        autonomy = await self._setting(svc, "autonomy_policy", DEFAULT_AUTONOMY, dict)
        k3_active = bool(last and (last.startswith("k3:") or last in ("escalate_ticket",)))
        confirm_pending = last == CONFIRM_ACTION
        # Way out of the ticket form: a question, a goodbye, "no" or "ya funciona" is not a slot answer.
        if (k3_active or confirm_pending) and not ts.ui_event and intent != policy.TICKET_CREATE and (
            intent == policy.CLOSING or ap.is_no(text) or ap.works_signal(text)
            or (text.strip().endswith("?") and not ap.parse_zone(text, zones))
        ):
            await self._trace(sid, ts, "actuar", action="k3_cancel", reason="la persona cambió de tema o cerró")
            k3_active = confirm_pending = False
            last = "k3_cancel"

        # ---------------- PERCIBIR
        # a click already produced typed facts (perceive_ui): never re-read its label with the regexes
        extracted = (
            ap.extract_facts(text, last, rs.actions, restart_guided="guide_reinicio" in done)
            if intent not in (policy.SMALLTALK, policy.CONSULTA) and not ui_turn
            else []
        )
        if not ui_turn and last and last.startswith("guide_") and ap.step_done_signal(text):
            cid = ub.stable_id(ts.session_id, last)  # voice/text "ya lo hice" = step_done on the current step
            props = await svc.ui_props(ts.session_id, cid)
            if props and props.get("current") and not ub.checklist_done(props):
                await self._advance_checklist(ts, sid, rs, cid, props, props["current"], "done")
                intent = policy.DIAGNOSTIC
        if not ui_turn and (k3_active or confirm_pending or intent == policy.TICKET_CREATE):
            slot = last[3:] if last and last.startswith("k3:") else None
            if z := ap.parse_zone(text, zones):
                extracted.append(("zona", z))
            free = intent == policy.TICKET_CREATE
            if (slot == "modalidad" or free) and (m := ap.parse_mode(text)):
                extracted.append(("modalidad", m))
            if (slot == "franja" or free) and (f := ap.parse_slot(text)):
                extracted.append(("franja", f))
            if contact and (slot == "contacto" or k3_active):
                self._contacts[ts.session_id] = contact  # consent: answered the optional contact question
        if not ui_turn and (dev := ap.extract_device(text)):
            self._device[ts.session_id] = dev
        llm_facts = False
        if (
            not extracted
            and not ui_turn
            and intent == policy.DIAGNOSTIC
            and await self._setting(svc, "agent_llm_extraction", False, bool)
        ):
            extracted = await self._llm_extract(svc, ts, text, rs)
            llm_facts = bool(extracted)
        certainty = 0.7 if llm_facts else (0.8 if ts.input_mode == "voice" else 1.0)
        stored = list(ts.ui_facts)
        from_text = []
        for k, v in extracted:
            try:
                await self.agent.upsert_fact(sid, k, v, "user_reported", certainty, turn_id=ts.turn_id)
                from_text.append({"key": k, "value": v, "source": "user_reported"})
            except DomainValidationError as e:
                log.info("fact %s=%r dropped: %s", k, v, e)
        if from_text:
            await self._trace(sid, ts, "percibir", facts_used=from_text,
                              reason=f"{len(from_text)} hecho(s) del mensaje ({'voz' if certainty < 1 else 'texto'})")  # fmt: skip
        stored += from_text

        # ---------------- CREER
        problem = intent in PROBLEM_INTENTS or bool(stored)
        hyps: list[dict[str, Any]] = []
        if problem or case:
            hyps = (await self.agent.recompute(sid, ts.turn_id)).hypotheses
            case = case or (await self.agent.get_case(sid) or {})
        facts = {f["key"]: f["value"] for f in await self.agent.active_facts(sid)} if case else {}
        if state == "nuevo" and problem:
            state = next_state(state, "problem_reported")
        if any(abs(h["cf"]) > 0 for h in hyps):
            state = next_state(state, "hypothesis_found")

        plan = AgentPlan(intent=intent if intent in (ap.WHY, ap.TICKET_FOLLOWUP) else None)
        plan.components = list(ts.ui_pending)
        plan.extra_tail = ts.ui_note
        own = autonomy.get("ticket_create_own_initiative", "con_confirmacion")
        new_verified = verified

        # ---------------- VERIFICAR (answers to a verification question / follow-up)
        awaiting = bool(
            last and (last.startswith("guide_") or (rs.actions.get(last) or {}).get("kind") == "guided_step")
        )
        works = ap.works_signal(text) or (awaiting and ap.is_yes(text))
        fails = not works and awaiting and (ap.is_no(text) or ap.failing_signal(text))
        if (
            state in ("resuelto", "escalado", "seguimiento")
            and (works or (state != "escalado" and ap.is_yes(text)))
            and not confirm_pending
        ):
            state = next_state(state, "followup_ok")
            await self._trace(
                sid, ts, "verificar", action="followup_ok", reason="la persona confirma que todo funciona: caso cerrado"
            )
            plan.tail = ap.phrase_instruction(
                "Qué bueno que todo funciona. Doy su caso por cerrado; si vuelve a fallar, aquí estoy.", level, trato, 2
            )
            plan.rag, plan.ticket_offer = False, False
        elif works and state in ("entendiendo", "diagnosticando", "intentando_solucion", "verificando"):
            state = next_state("verificando", "verified_working")
            new_verified = True
            await self._trace(
                sid, ts, "verificar", action="verified_working", reason="la persona confirma que ya funciona"
            )
            hours = await self._setting(svc, "followup_hours", DEFAULT_FOLLOWUP, dict)
            after = hours.get("after_step", 0.25) if isinstance(hours.get("after_step"), int | float) else 0.25
            await self._schedule("followup", session_id=ts.session_id, run_at=datetime.now(UTC) + timedelta(hours=after),
                                 payload={"reason": "verified"})  # fmt: skip
            plan.tail = ap.resolved_instruction(level, trato, last if awaiting else None)
            plan.rag, plan.ticket_offer = False, False
            plan.card, plan.report = {}, True
        elif fails and state in ("intentando_solucion", "verificando"):
            state = next_state("verificando", "still_failing")
            await self._trace(sid, ts, "verificar", action="still_failing", reason="la persona dice que sigue fallando")

        if not ui_turn and ap.REDO_DIAGRAM.search(text):  # "arma el diagrama otra vez": fresh topology, no LLM
            plan.components.append(self._topology(ts.session_id, facts))
            ts.ui_fixed = "Aquí está el diagrama actualizado con lo que me ha contado."
            self._topo[ts.session_id] = (ub.topology_signature(plan.components[-1]), self._turn_no.get(ts.session_id, 0))

        # ---------------- DECIDIR + ACTUAR
        if plan.tail is None and intent == policy.CONSULTA and not ui_turn and not (k3_active or confirm_pending):
            plan.tail, plan.rag, plan.ticket_offer = ap.consulta_instruction(level, trato), True, False
            plan.max_tokens = policy.max_tokens_for(intent, ts.voice, overrides=ts.token_limits)
        forced = self._forced(ts, rs, facts, own) if plan.tail is None else None
        if plan.tail is None:
            if ts.ui_fixed:  # e.g. next checklist step: deterministic, no LLM
                plan.fixed_text, plan.rag = ts.ui_fixed, False
            elif ts.ui_step_failed is not None:
                state = await self._step_failed(svc, sid, ts, plan, hyps, state, done, own, level, trato)
            elif ts.ui_verify and forced is None:
                state = await self._verify_after_checklist(sid, ts, plan, rs, hyps, state, level, trato)
            elif forced is not None:
                state = await self._act(
                    svc, sid, ts, plan, rs, forced, labels, level, trato, state, facts, hyps, taken, zones
                )
            elif confirm_pending and (ts.confirm is not None or ap.is_yes(text) or ap.is_no(text)):
                accepted = ts.confirm if ts.confirm is not None else ap.is_yes(text)
                if accepted:
                    state = await self._escalate_create(
                        svc, ts, sid, plan, rs, hyps, facts, snap, user_texts, level, trato, state
                    )
                else:
                    self._contacts.pop(ts.session_id, None)
                    await self._decide(
                        sid,
                        ts,
                        "escalate:cancelled",
                        "la persona no confirmó el registro",
                        None,
                        hyps,
                        "registro cancelado",
                    )
                    plan.tail = ap.phrase_instruction(
                        "De acuerdo, no registro el caso. Si cambia de opinión o necesita algo más, dígamelo.",
                        level,
                        trato,
                        2,
                    )
                    plan.rag, plan.ticket_offer = False, False
            elif intent == ap.WHY:
                await self._why(sid, ts, plan, text, hyps, level, trato, rs, th, labels)
            elif intent == ap.TICKET_FOLLOWUP:
                await self._followup(sid, ts, plan, level, trato, hyps, labels, state)
            elif intent == policy.TICKET and (m := ap.TICKET_NO.search(text)) and self.tickets is not None:
                try:
                    tl = await self.tickets.timeline(m.group(0).upper())
                    plan.extra_tail = "DATOS DEL TICKET (úselos tal cual, sin inventar): " + ap.followup_text(tl)
                except Exception:
                    pass
            elif k3_active or intent == policy.TICKET_CREATE or confirm_pending:
                why = (
                    "la persona pidió un técnico o registrar el caso"
                    if intent == policy.TICKET_CREATE
                    else "continúa el registro del caso"
                )
                await self._escalation_step(
                    svc,
                    ts,
                    sid,
                    plan,
                    facts,
                    hyps,
                    level,
                    trato,
                    autonomy.get("ticket_create_on_request", "con_aviso"),
                    why,
                    taken,
                    zones,
                )
            elif intent in (policy.DIAGNOSTIC, policy.OUTPUT) and problem and not (
                # resolved / escalated: no more diagnostic questions or restart guides unless it fails again
                state in ("resuelto", "escalado", "cerrado") and not ap.failing_signal(ts.user_text or "")
            ):
                d = ap.select_action(rs, hyps, th, done, facts, ts.audience, autonomy, new_verified)
                state = await self._act(
                    svc, sid, ts, plan, rs, d, labels, level, trato, state, facts, hyps, taken, zones
                )

        # ---------------- lifecycle + case_update
        if case or problem:
            if state != (case.get("state") or "nuevo") or new_verified != verified:
                try:
                    await self.agent.set_case(sid, state=state, verified=new_verified)
                except Exception as e:
                    log.warning("could not update case: %s", type(e).__name__)
            top = [{"key": h["key"], "label": labels.get(h["key"], h["key"]), "cf": round(h["cf"], 3)}
                   for h in sorted(hyps, key=lambda h: -h["cf"]) if h["cf"] > 0][:3]  # fmt: skip
            plan.case_update = {"type": "case_update", "turn_id": ts.turn_id, "state": state, "top_hypotheses": top,
                                "verified": new_verified}  # fmt: skip
            plan.snapshot = {"case_state": state, "verified": new_verified, "top_hypotheses": top,
                             "facts_new": [f["key"] for f in stored]}  # fmt: skip
        try:
            await self._finish_ui(ts, sid, plan, state, hyps, labels, th, facts, case)
        except Exception as e:  # components never break a turn
            log.warning("ui components skipped: %s: %s", type(e).__name__, e)
        return plan

    # ------------------------------------------------------------------ decision -> action (+ its component)
    def _forced(self, ts: "TurnState", rs: Any, facts: dict, own: str) -> ap.Decision | None:
        """Action chosen on screen (topology node, compare card, discarded photo) or the verification of a
        completed checklist (its `verify_with` question)."""
        aid, reason = ts.ui_force, "la persona lo eligió en pantalla"
        if ts.ui_verify:
            spec = rs.actions.get(ts.ui_verify) or {}
            vw = spec.get("verify_with")
            vspec = rs.actions.get(vw or "") or {}
            if vspec.get("kind") == "ask" and vspec.get("fact") not in facts:
                title = (spec.get("ui") or {}).get("title") or ts.ui_verify
                aid, reason = vw, f"comprobar si «{title}» resolvió el problema"
            else:
                return None
        if not aid:
            return None
        if aid == "offer_escalation":
            return ap.Decision("offer_escalation", aid, "la persona eligió que lo revise un técnico", own)
        if aid == "escalate_ticket":
            return ap.Decision("escalate", aid, "la persona eligió que lo revise un técnico", own)
        spec = rs.actions.get(aid) or {}
        if spec.get("kind") == "ask":
            return ap.Decision("ask", aid, reason, spec.get("autonomy", "autonomo"), fact=spec.get("fact"))
        if spec.get("kind") == "guided_step" or aid.startswith("guide_"):
            return ap.Decision("guide", aid, reason, spec.get("autonomy", "autonomo"), may_resolve=bool(spec.get("may_resolve", True)))  # fmt: skip
        return None

    async def _act(self, svc, sid, ts, plan, rs, d, labels, level, trato, state, facts, hyps, taken, zones) -> str:  # type: ignore[no-untyped-def]
        if d.kind == "escalate":
            await self._escalation_step(
                svc, ts, sid, plan, facts, hyps, level, trato, d.autonomy, d.reason, taken, zones
            )
            return state
        if d.kind == "none":
            await self._trace(sid, ts, "decidir", action=None, reason=d.reason)
            return state
        explanation = ""
        if d.kind == "explain" and d.target:
            try:
                explanation = (await self.agent.explain(sid, d.target))["text"]
            except NotFoundError:
                explanation = labels.get(d.target, d.target)
        spec = rs.actions.get(d.action or "") or {}
        comp = None
        if d.kind == "ask":
            comp = ub.quick_replies_for(
                spec, rs.facts, ap.ASK_TEXT.get(d.action or ""), id=ub.stable_id(ts.session_id, d.action)
            )
        elif d.kind == "guide":
            comp = ub.checklist_for(d.action or "", spec, id=ub.stable_id(ts.session_id, d.action))
        title = comp["props"]["title"] if comp and comp["component"] == "checklist" else None
        plan.tail = ap.decision_instruction(d, level, ts.voice, trato, explanation, title)
        plan.ticket_offer = d.kind == "offer_escalation" or "offer_escalation" in d.marks
        plan.context = d.kind in ("guide", "explain") and not title
        if comp:
            plan.components.append(comp)
        if d.kind == "explain":
            plan.card, plan.bars = {}, True
        if plan.ticket_offer and await svc._session_ticket(ts.session_id) is None:
            plan.components.append(ub.yes_no_replies("registrar_caso", ap.OFFER_Q, "Sí, que lo revise un técnico",
                                                     "No, por ahora no", id=ub.stable_id(ts.session_id, "offer")))  # fmt: skip
        done_reason = {"ask": "pregunta enviada", "guide": "paso guiado enviado con pedido de verificación",
                       "explain": "explicación enviada", "offer_escalation": "se ofreció registrar el caso"}[d.kind]  # fmt: skip
        if comp:
            done_reason += f" (con {comp['component']})"
        await self._decide(sid, ts, d.action or d.kind, d.reason, d.autonomy, hyps, done_reason)
        for extra in d.marks:
            await self._trace(
                sid, ts, "actuar", action=extra, reason="incluido en la misma respuesta", autonomy=d.autonomy
            )
        if d.kind == "guide" and d.may_resolve:
            state = next_state(next_state(state, "guided_step_may_resolve"), "verification_requested")
        return state

    async def _step_failed(self, svc, sid, ts, plan, hyps, state, done, own, level, trato) -> str:  # type: ignore[no-untyped-def]
        if state in ("intentando_solucion", "verificando"):
            state = next_state("verificando", "still_failing")
        await self._trace(
            sid, ts, "verificar", action="still_failing", reason=f"no pudo completar el paso «{ts.ui_step_failed}»"
        )
        offer = "offer_escalation" not in done and await svc._session_ticket(ts.session_id) is None
        msg = "No se preocupe si no pudo hacer ese paso." + (
            f" {ap.OFFER_Q}" if offer else " Cuénteme qué pasó y buscamos otra forma."
        )
        if offer:
            await self._decide(
                sid,
                ts,
                "offer_escalation",
                "no pudo completar un paso guiado",
                own,
                hyps,
                "se ofreció registrar el caso",
            )
            plan.components.append(ub.yes_no_replies("registrar_caso", ap.OFFER_Q, "Sí, que lo revise un técnico",
                                                     "No, por ahora no", id=ub.stable_id(ts.session_id, "offer")))  # fmt: skip
        plan.tail, plan.fallback_text = ap.phrase_instruction(msg, level, trato, 2), msg
        plan.rag, plan.ticket_offer = False, offer
        return state

    async def _verify_after_checklist(self, sid, ts, plan, rs, hyps, state, level, trato) -> str:  # type: ignore[no-untyped-def]
        spec = rs.actions.get(ts.ui_verify or "") or {}
        title = (spec.get("ui") or {}).get("title") or (ts.ui_verify or "").removeprefix("guide_").replace("_", " ")
        # decidir only: the guided action stays the last one executed, so "sí/no" still answers it
        await self._trace(sid, ts, "decidir", action="verify", reason=f"terminó los pasos de «{title}»: comprobar si ya funciona",
                          autonomy="autonomo", hypotheses=[{"key": h["key"], "cf": h["cf"], "status": h["status"]} for h in hyps])  # fmt: skip
        if state in ("diagnosticando", "intentando_solucion"):
            state = "verificando"
        msg = f"Muy bien, terminó los pasos. {ap.VERIFY_SHORT}"
        plan.tail, plan.fallback_text = ap.phrase_instruction(msg, level, trato, 2), msg
        plan.rag, plan.ticket_offer = False, False
        plan.components.append(ub.yes_no_replies("verificacion", ap.VERIFY_SHORT, "Sí, ya funciona", "No, sigue igual",
                                                 id=ub.stable_id(ts.session_id, "verify", ts.ui_verify)))  # fmt: skip
        return state

    def _topology(self, session_id: str, facts: dict[str, Any]) -> dict[str, Any]:
        f = {**facts, "equipo_afectado": self._device[session_id]} if session_id in self._device else facts
        return ub.topology_from_facts(f, id=ub.stable_id(session_id, "topology"))

    async def _finish_ui(self, ts, sid, plan, state, hyps, labels, th, facts, case) -> None:  # type: ignore[no-untyped-def]
        """Components that depend on the final state of the cycle + the LLM hint."""
        n = self._turn_no[ts.session_id] = self._turn_no.get(ts.session_id, 0) + 1
        view = {"state": state, "hypotheses": [{**h, "label": labels.get(h["key"], h["key"])} for h in hyps],
                "ticket": {"ticket_number": case.get("ticket_number"), "status": case.get("ticket_status")}
                if case.get("ticket_number") else None}  # fmt: skip
        if plan.card is not None:
            plan.components.append(
                ub.case_card(view, plan.card.get("ticket"), id=ub.stable_id(ts.session_id, "case_card"))
            )
        if plan.bars and hyps:
            plan.components.append(ub.hypothesis_bars(view["hypotheses"], th, id=ub.stable_id(ts.session_id, "bars")))
        if plan.report:
            plan.components.append(ub.report_card(ts.session_id, id=ub.stable_id(ts.session_id, "report")))
        if facts and len(plan.components) < 2 and plan.confirm is None and ub.known_network_facts(facts) >= 2:
            topo = self._topology(ts.session_id, facts)
            sig, prev = ub.topology_signature(topo), self._topo.get(ts.session_id)
            if prev is None or (sig != prev[0] and n - prev[1] >= 3):
                plan.components.append(topo)
                self._topo[ts.session_id] = (sig, n)
        hint = ap.ui_hint([c for c in plan.components if c not in ts.ui_pending or c["component"] == "image_annotated"])
        if hint and plan.fixed_text is None:
            if plan.tail:
                plan.tail += "\n\n" + hint
            else:
                plan.extra_tail = (plan.extra_tail + "\n\n" + hint).strip()

    # ------------------------------------------------------------------ why / follow-up
    async def _why(
        self, sid: UUID, ts: "TurnState", plan: AgentPlan, text: str, hyps: list, level: str, trato: str,
        rs: Any = None, th: dict | None = None, labels: dict | None = None,
    ) -> None:  # fmt: skip
        last_ask = None
        try:
            for t in reversed(await self.agent.repo.list_trace(sid, 50)):
                if t["phase"] == "decidir" and t.get("action") and t["action"].startswith(("ask_", "guide_", "k3:")):
                    last_ask = t
                    break
        except Exception:
            last_ask = None
        explanation = None
        if (any(h["cf"] != 0 for h in hyps) and not (re.search(r"pregunt", text, re.I) and last_ask)) or ts.ui_target:
            try:
                explanation = await self.agent.explain(sid, ts.ui_target)
            except NotFoundError:
                explanation = None
        expl = ap.why_text(text, last_ask, explanation)
        await self._decide(
            sid,
            ts,
            "explain:why",
            "la persona pidió el porqué",
            "autonomo",
            hyps,
            "explicación desde la traza y las reglas",
        )
        plan.tail = ap.phrase_instruction(expl, level, trato, 3)
        plan.fallback_text = expl
        plan.rag, plan.ticket_offer = False, False
        plan.max_tokens = policy.max_tokens_for("clarify", ts.voice, overrides=ts.token_limits)
        if rs is not None and hyps:  # U-5: certainty bars + the path questions -> answers -> rules -> hypothesis
            try:
                labelled = [{**h, "label": (labels or {}).get(h["key"], h["key"])} for h in hyps]
                plan.components.append(ub.hypothesis_bars(labelled, th or {}, id=ub.stable_id(ts.session_id, "bars")))
                plan.components.append(ub.diagnosis_flow_from_trace(
                    await self.agent.repo.list_trace(sid, 50), rs, await self.agent.active_facts(sid), hyps,
                    ap.ASK_TEXT, id=ub.stable_id(ts.session_id, "flow")))  # fmt: skip
            except Exception as e:
                log.warning("why components skipped: %s", type(e).__name__)

    async def _followup(
        self, sid: UUID, ts: "TurnState", plan: AgentPlan, level: str, trato: str,
        hyps: list | None = None, labels: dict | None = None, state: str = "escalado",
    ) -> None:  # fmt: skip
        plan.rag = False
        plan.max_tokens = policy.max_tokens_for("plantilla", ts.voice, overrides=ts.token_limits)
        tl = None
        if self.tickets is not None:
            try:
                tl = await self.tickets.mine(ts.session_id)
            except NotFoundError:
                tl = None
        if tl is None:
            msg = "No encuentro un caso registrado a su nombre. Si quiere, puedo registrar uno para que un técnico lo revise."
            plan.tail, plan.fallback_text, plan.ticket_offer = ap.phrase_instruction(msg, level, trato, 2), msg, True
            await self._decide(
                sid, ts, "ticket_followup", "la persona preguntó por su caso", "autonomo", [], "sin ticket registrado"
            )
            return
        msg = ap.followup_text(tl)
        plan.tail, plan.fallback_text, plan.ticket_offer = ap.phrase_instruction(msg, level, trato, 4), msg, False
        try:  # A1 visual: case timeline + card with the ticket and technician
            view = await self.agent.case_view(sid)
            plan.components.append(ub.timeline(view, tl, self._guide_titles(await self.agent.load_ruleset()),
                                               id=ub.stable_id(ts.session_id, "timeline")))  # fmt: skip
            plan.components.append(ub.case_card(view, tl, id=ub.stable_id(ts.session_id, "case_card")))
        except Exception as e:
            log.warning("followup components skipped: %s", type(e).__name__)
        await self._decide(sid, ts, "ticket_followup", "la persona preguntó por su caso", "autonomo", [],
                           f"estado de {ap._get(tl, 'ticket_number')} informado")  # fmt: skip

    # ------------------------------------------------------------------ K3 + confirmation + creation
    async def _escalation_step(
        self, svc, ts, sid, plan, facts, hyps, level, trato, autonomy, reason, taken, zones
    ) -> None:  # type: ignore[no-untyped-def]
        plan.rag, plan.ticket_offer = False, None
        existing = await svc._session_ticket(ts.session_id)
        if existing is not None:
            plan.tail = policy.ticket_created_instruction(existing[0], existing[1], False, trato)
            plan.ticket_offer = False
            return
        if not any(a.startswith(("k3:", "escalate_ticket")) for a in taken[-1:]):
            await self._trace(sid, ts, "decidir", action="escalate_ticket", reason=reason, autonomy=autonomy)
        questions = [
            q for q in await self._setting(svc, "assignment_questions", DEFAULT_QUESTIONS, list) if isinstance(q, str)
        ]
        known = {k: facts.get(k) for k in ("zona", "modalidad", "franja")}
        asked = {a for a in taken if a.startswith("k3:")}
        if ts.session_id in self._contacts or "k3:form" in asked:  # the form already offered the optional contact
            asked.add("k3:contacto")
        missing = ap.missing_slots(questions, known, asked)
        if missing:
            slot = missing[0]
            q = ap.slot_question(slot, zones, known)
            if not asked:  # first K3 step: one form instead of 4 questions (text answers keep working)
                form = ub.assignment_form(zones, known, ts.session_id in self._contacts, ap.zone_label,
                                          id=ub.stable_id(ts.session_id, "form"))  # fmt: skip
                if form:
                    plan.components.append(form)
                    q = f"Puede llenar el formulario en pantalla o responderme aquí. {q}"

            await self._decide(sid, ts, f"k3:{slot}", f"falta el dato «{slot}» para asignar el técnico", autonomy, hyps,
                               f"pregunta de asignación: {slot}")  # fmt: skip
            plan.fixed_text = q
            return
        summary = ap.confirm_summary(known)
        await self._decide(sid, ts, CONFIRM_ACTION, "registrar un ticket requiere confirmación", "con_confirmacion", hyps,
                           "confirmación pedida")  # fmt: skip
        plan.fixed_text = summary
        plan.confirm = {"type": "confirm_request", "turn_id": ts.turn_id, "action": "ticket_create", "summary": summary}

    async def _escalate_create(self, svc, ts, sid, plan, rs, hyps, facts, snap, user_texts, level, trato, state) -> str:  # type: ignore[no-untyped-def]
        plan.rag, plan.ticket_offer = False, False
        plan.max_tokens = policy.max_tokens_for("plantilla", ts.voice, overrides=ts.token_limits)
        await svc._progress(ts, "ticket")
        existing = await svc._session_ticket(ts.session_id)
        if existing is not None:
            plan.tail = policy.ticket_created_instruction(existing[0], existing[1], False, trato)
            return state
        known = {k: facts.get(k) for k in ("zona", "modalidad", "franja")}
        args = policy.build_ticket(user_texts, snap)
        labels = {k: v.get("label", k) for k, v in rs.hypotheses.items()}
        ranked = sorted((h for h in hyps if h["cf"] > 0), key=lambda h: (h["status"] != "aceptada", -h["cf"]))
        if ranked:
            args["category"] = rs.hypotheses.get(ranked[0]["key"], {}).get("category") or args["category"]
            if ranked[0]["status"] == "aceptada" and ranked[0]["key"].startswith("H1"):
                args["priority"] = "high"
        handoff = ap.handoff_summary(ts.session_id, snap, await self.agent.active_facts(sid), ranked, labels,
                                     await self._source_titles(svc, ts.session_id), known, render_fact)  # fmt: skip
        tool_args = {"category": args["category"], "priority": args["priority"], "zone": known["zona"],
                     "service_mode": known["modalidad"], "preferred_slot": known["franja"]}  # fmt: skip
        await ts.emit({"type": "tool_call", "turn_id": ts.turn_id, "name": "create_ticket", "arguments": tool_args})
        try:
            assert self.tickets is not None
            t = await self.tickets.create_incident_with_ticket(
                title=args["title"], description=args["description"], category=args["category"], priority=args["priority"],
                session_id=ts.session_id, evidence=[{"text": e, "origin": "conversation"} for e in args["evidence"]],
                zone=known["zona"], service_mode=known["modalidad"], preferred_slot=known["franja"], handoff_summary=handoff,
            )  # fmt: skip
        except Exception as e:
            log.warning("agent ticket creation failed: %s", type(e).__name__)
            await ts.emit(
                {"type": "tool_result", "turn_id": ts.turn_id, "name": "create_ticket", "ok": False, "summary": "error"}
            )
            ts.tool_calls, ts.tool_errors = ts.tool_calls + 1, ts.tool_errors + 1
            plan.tail = ap.phrase_instruction(
                "No pude registrar el caso por un error del sistema. Le pido disculpas; por ahora puede contactar a su proveedor.",
                level,
                trato,
                2,
            )
            return state
        number = t.ticket_number
        summary = f"ticket {number} creado"
        ts.tool_calls += 1
        await ts.emit(
            {"type": "tool_result", "turn_id": ts.turn_id, "name": "create_ticket", "ok": True, "summary": summary}
        )
        await svc.repo.add_message(ts.session_id, "tool", json.dumps(
            {"name": "create_ticket", "arguments": tool_args, "ok": True, "summary": summary, "sources": [], "auto": True},
            ensure_ascii=False, default=str), ts.input_mode, None)  # fmt: skip
        if contact := self._contacts.pop(ts.session_id, None):
            try:
                await self.tickets.set_contact(number, contact)
            except Exception as e:
                log.warning("contact not stored: %s", type(e).__name__)
        specialties = None
        if t.technician_name:
            try:
                tl = await self.tickets.timeline(number)
                specialties = tl.technician.specialties if tl.technician else None
            except Exception:
                specialties = None
        state = next_state(state, "escalated")
        case = {}
        try:
            case = await self.agent.set_case(sid, state=state, ticket_number=number)
        except Exception as e:
            log.warning("could not link ticket to case: %s", type(e).__name__)
        tid = str(case["ticket_id"]) if case.get("ticket_id") else None
        await self._schedule("sla_watch", session_id=ts.session_id, ticket_id=tid, run_at=datetime.now(UTC),
                             payload={"ticket_id": tid, "ticket_number": number})  # fmt: skip
        hours = await self._setting(svc, "followup_hours", DEFAULT_FOLLOWUP, dict)
        after = hours.get("after_ticket", 24) if isinstance(hours.get("after_ticket"), int | float) else 24
        await self._schedule("followup", session_id=ts.session_id, ticket_id=tid,
                             run_at=datetime.now(UTC) + timedelta(hours=after), payload={"ticket_number": number})  # fmt: skip
        why = getattr(t, "assignment_reason", None) or ""
        await self._decide(sid, ts, "ticket:create", "la persona confirmó el registro", "con_confirmacion", hyps,
                           f"{summary}; {why}".strip("; "))  # fmt: skip
        msg = ap.created_text(
            number, t.technician_name, specialties, bool(getattr(t, "queued", False)) or not t.technician_name, known
        )
        plan.tail, plan.fallback_text = ap.phrase_instruction(msg, level, trato, 3), msg
        plan.card, plan.report = {"ticket": t}, True
        return state

    async def _source_titles(self, svc: "ConversationService", session_id: str) -> list[str]:
        out: list[str] = []
        try:
            for m in await svc.repo.list_messages(session_id) or []:
                if m.get("role") != "tool":
                    continue
                for s in json.loads(m["content"]).get("sources") or []:
                    out.append(s.get("title", "") + (f" › {s['section']}" if s.get("section") else ""))
        except Exception:
            return out
        return [x for x in out if x]

    # ------------------------------------------------------------------ optional LLM extraction (flag, default off)
    async def _llm_extract(
        self, svc: "ConversationService", ts: "TurnState", text: str, rs: Any
    ) -> list[tuple[str, Any]]:
        if len(text) > 200:
            return []
        props: dict[str, Any] = {}
        for k, spec in rs.facts.items():
            t = spec.get("type")
            props[k] = (
                {"type": "string", "enum": spec["values"]} if t == "enum"
                else {"type": "boolean"} if t == "bool" else {"type": "integer"} if t == "int" else {"type": "string"}
            )  # fmt: skip
        schema = {"type": "object", "properties": {"facts": {"type": "array", "items": {
            "type": "object", "properties": {"key": {"type": "string", "enum": list(props)}, "value": {}},
            "required": ["key", "value"]}}}, "required": ["facts"]}  # fmt: skip
        catalog = "; ".join(
            f"{k} ({'|'.join(map(str, v['values'])) if v.get('type') == 'enum' else v.get('type')})"
            for k, v in rs.facts.items()
        )
        prompt = [
            {"role": "system", "content": (
                "Extrae SOLO hechos del catálogo que la persona afirma explícitamente. Responda SOLO JSON con la forma "
                f'{{"facts":[{{"key":"...","value":...}}]}}. Catálogo: {catalog}.')},
            {"role": "user", "content": text},
        ]  # fmt: skip
        raw = ""
        try:
            async with svc.lease("llm", ts) as lease:
                if lease.adapter.kind == "llamacpp":
                    fmt: dict[str, Any] = {"type": "json_schema", "json_schema": {"name": "facts", "schema": schema}}
                else:
                    fmt = {"type": "json_object"}
                extra = {**(lease.profile.params.get("extra_body") or {}), "response_format": fmt}
                params = {
                    **lease.profile.params,
                    "max_tokens": policy.max_tokens_for("extraccion", False, overrides=ts.token_limits),
                    "temperature": 0,
                    "extra_body": extra,
                }
                async for d in lease.adapter.chat(
                    lease.profile.model_ref, prompt, tools=None, stream=True, params=params
                ):
                    raw += d.content or ""
            items = json.loads(raw).get("facts") or []
        except Exception as e:
            log.info("llm extraction skipped: %s", type(e).__name__)
            return []
        return [(i["key"], i["value"]) for i in items if isinstance(i, dict) and i.get("key") in rs.facts]

    # ------------------------------------------------------------------ generative UI: perception of clicks / media
    @staticmethod
    def _guide_titles(rs: Any) -> dict[str, str]:
        return {a: (v.get("ui") or {}).get("title") for a, v in rs.actions.items() if (v.get("ui") or {}).get("title")}

    async def _ui_fact(
        self, sid: UUID, ts: "TurnState", key: str, value: Any, certainty: float, source: str, reason: str,
        evidence: UUID | None = None,
    ) -> dict | None:  # fmt: skip
        try:
            await self.agent.upsert_fact(sid, key, value, source, certainty, evidence_ref=evidence, turn_id=ts.turn_id)
        except DomainValidationError as e:
            log.info("ui fact %s=%r dropped: %s", key, value, e)
            return None
        f = {"key": key, "value": value, "source": source}
        ts.ui_facts.append(f)
        await self._trace(sid, ts, "percibir", facts_used=[f], reason=reason)
        return f

    async def _advance_checklist(
        self, ts: "TurnState", sid: UUID, rs: Any, cid: str, props: dict, step_id: str, status: str
    ) -> str:
        """step_done / step_failed (click or "ya lo hice"): re-emit the checklist with the same id; when every step
        is done the action's `sets` fact is stored and the verification follows."""
        step = next(s for s in props["steps"] if s["id"] == step_id)
        new = ub.update_checklist(props, step_id, status)
        ts.ui_pending.append(ub.checklist_envelope(new, id=cid))
        what = "hecho" if status == "done" else "no se pudo hacer"
        await self._trace(sid, ts, "percibir", reason=f"interacción con componente: paso «{step['text']}» {what}")
        ts.ui_intent = policy.DIAGNOSTIC
        if status == "failed":
            ts.ui_step_failed = step["text"]
            return f"No pude hacer este paso: {step['text']}"
        if ub.checklist_done(new):
            spec = rs.actions.get(props["action_id"]) or {}
            if (k := spec.get("sets")) and k in rs.facts:
                await self._ui_fact(
                    sid, ts, k, True, 1.0, "user_reported", f"interacción con componente: completó «{props['title']}»"
                )
            ts.ui_verify = props["action_id"]
            return f"Ya hice todos los pasos de «{props['title']}»."
        steps = new["steps"]
        i = next(n for n, s in enumerate(steps) if s["id"] == new["current"])
        ts.ui_fixed = f"Muy bien. Paso {i + 1} de {len(steps)}: {steps[i]['text']}"
        return f"Ya lo hice: {step['text']}"

    async def perceive_ui(self, svc: "ConversationService", ts: "TurnState", ev: dict) -> str | None:
        """ui_event -> facts/actions (contract §2). Returns the text stored as the person's message; None = invalid
        (error emitted); "" = metric-only event (no reply)."""
        sid = UUID(ts.session_id)
        comp, action = ev.get("component") or "", ev.get("action") or ""
        payload, cid = ev.get("payload") or {}, ev.get("component_id") or ""

        async def bad(detail: str) -> None:
            await ts.emit({"type": "error", "turn_id": ts.turn_id, "detail": detail})

        if action not in EVENT_ACTIONS.get(comp, ()):
            await bad(f"acción «{action}» no válida para el componente «{comp}»")
            return None
        rs = await self.agent.load_ruleset()
        why = "interacción con componente"
        props = await svc.ui_props(ts.session_id, cid) or {}

        if action == "quick_reply":
            fact, value = payload.get("fact"), payload.get("value")
            opts = props.get("options") or ub.options_for_fact(fact or "", rs.facts.get(fact or "") or {}) or []
            # The UI stringifies option values (true -> "true"): map back to the option's real value.
            hit = next((o for o in opts if o.get("value") == value), None) or next(
                (o for o in opts if value is not None and str(o.get("value")).lower() == str(value).lower()), None
            )
            if hit is not None:
                value = hit.get("value")
            label = hit["label"] if hit else None
            if fact in rs.facts:
                ts.ui_intent = policy.DIAGNOSTIC
                if value is None:
                    await self._trace(sid, ts, "percibir", reason=f"{why}: no sabe ({fact})")
                    return ub.UNKNOWN_LABEL
                if await self._ui_fact(sid, ts, fact, value, 1.0, "user_reported", why) is None:
                    await bad(f"valor fuera del dominio de «{fact}»")
                    return None
                return label or render_fact(fact, value)
            text = ap.PSEUDO_ANSWERS.get((fact, value)) or label  # not a catalog fact: answer as plain text
            if not text:
                await bad("respuesta no reconocida")
                return None
            return text

        if action in ("step_done", "step_failed"):
            aid = payload.get("action_id") or props.get("action_id")
            cid = cid or ub.stable_id(ts.session_id, aid)
            if props.get("action_id") != aid:
                props = ub.checklist_props(aid or "", rs.actions.get(aid or "") or {}) or {}
            if not props or payload.get("step_id") not in {s["id"] for s in props["steps"]}:
                await bad("paso desconocido")
                return None
            return await self._advance_checklist(
                ts, sid, rs, cid, props, payload["step_id"], "done" if action == "step_done" else "failed"
            )

        if action == "form_submit":
            values = payload.get("values") or {}
            zones = [
                z for z in await self._setting(svc, "zones", DEFAULT_ZONES, list) if isinstance(z, str)
            ] or DEFAULT_ZONES
            z = values.get("zona")
            z = z if z in zones else ap.parse_zone(str(z or ""), zones)
            parts, got = [], []
            for key, val, lbl in (
                ("zona", z, ap.zone_label(z) if z else None),
                ("modalidad", values.get("modalidad"), ub.ENUM_LABELS["modalidad"].get(values.get("modalidad") or "")),
                ("franja", values.get("franja"), ub.ENUM_LABELS["franja"].get(values.get("franja") or "")),
            ):
                if val and lbl:
                    try:
                        await self.agent.upsert_fact(sid, key, val, "user_reported", 1.0, turn_id=ts.turn_id)
                        got.append({"key": key, "value": val, "source": "user_reported"})
                        parts.append(lbl.lower() if key != "zona" else lbl)
                    except DomainValidationError:
                        pass
            contact = ap.find_contact(str(values.get("contacto") or ""))
            if contact:
                self._contacts[ts.session_id] = contact  # consent given in the form; never persisted in text
            await self._trace(sid, ts, "percibir", facts_used=got, reason=f"{why}: formulario de asignación")
            await self._trace(
                sid, ts, "actuar", action="k3:form", reason="datos de asignación recibidos por formulario"
            )
            ts.ui_intent = policy.TICKET_CREATE
            return (
                "Datos para el técnico: "
                + (", ".join(parts) or "sin preferencias")
                + (" (con contacto)" if contact else "")
            )

        if action in ("case_action", "why"):
            what = payload.get("what") if action == "case_action" else "why"
            await self._trace(sid, ts, "percibir", reason=f"{why}: {action} {what}")
            if action == "why" and payload.get("key") in rs.hypotheses:
                ts.ui_intent, ts.ui_target = ap.WHY, payload["key"]
                return f"¿Por qué «{rs.hypotheses[payload['key']]['label']}»?"
            if what in ("why", "open_diagnosis"):
                ts.ui_intent = ap.WHY
                return "¿Por qué cree eso?" if what == "why" else "¿Cómo llegó a ese diagnóstico?"
            if what == "open_ticket":
                ts.ui_intent = ap.TICKET_FOLLOWUP
                return "¿Qué pasó con mi caso?"
            if what == "report":
                ts.ui_pending.append(ub.report_card(ts.session_id, id=ub.stable_id(ts.session_id, "report")))
                ts.ui_fixed = "Aquí tiene el informe de su caso, listo para descargar o imprimir."
                return "Quiero el informe del caso."
            await bad("acción de tarjeta desconocida")
            return None

        if action == "topology_node":
            node = payload.get("node_id")
            n = next((x for x in props.get("nodes") or [] if x.get("id") == node), None)
            label = (n or {}).get("label") or {"isp": "el proveedor", "modem": "el módem", "router": "el router",
                                               "devices": "los otros equipos", "pc": "este equipo"}.get(node, "esa parte de la red")  # fmt: skip
            facts = {f["key"] for f in await self.agent.active_facts(sid)}
            turn = self._turn_no.get(ts.session_id, 0)
            prev = self._node_clicks.get(ts.session_id)
            self._node_clicks[ts.session_id] = (str(node), turn)
            if prev and prev[0] == node and turn - prev[1] <= 2:  # same node asked again: short answer, no new turn work
                detail = (n or {}).get("detail") or "todavía no tengo más datos de esa parte"
                ts.ui_intent = policy.DIAGNOSTIC
                ts.ui_fixed = f"Ya le conté sobre {label.lower()}: {detail[:1].lower() + detail[1:]}."
                return f"¿Qué pasa con {label.lower()}?"
            for aid in NODE_ACTIONS.get(node, ()):
                spec = rs.actions.get(aid) or {}
                if spec.get("kind") == "ask" and spec.get("fact") not in facts:
                    ts.ui_force = aid
                    break
            await self._trace(sid, ts, "percibir", reason=f"{why}: preguntó por {label.lower()}")
            ts.ui_intent = policy.DIAGNOSTIC
            if ts.ui_force is None:
                detail = (n or {}).get("detail") or "todavía no tengo datos de esa parte"
                ts.ui_fixed = f"Sobre {label.lower()}: {detail[:1].lower() + detail[1:]}."
            return f"¿Qué pasa con {label.lower()}?"

        if action == "compare_choose":
            oid = payload.get("option_id")
            opt = next((o for o in props.get("options") or [] if o.get("id") == oid), None)
            if not (oid in rs.actions or oid in ("offer_escalation", "escalate_ticket")):
                await bad("opción desconocida")
                return None
            ts.ui_force, ts.ui_intent = oid, policy.DIAGNOSTIC
            await self._trace(sid, ts, "percibir", reason=f"{why}: eligió {oid}")
            return f"Elijo: {(opt or {}).get('title') or oid.replace('_', ' ')}"

        if action == "image_confirm":
            mid = str(payload.get("media_id") or props.get("media_id") or "")
            media = await self._media(svc, mid, ts.session_id)
            found, _ = ub.facts_from_analysis(_analysis(media))
            cf = props.get("confirm_fact")
            if not found and isinstance(cf, dict) and cf.get("key"):
                found = [(cf["key"], cf.get("value"))]
            if not found:
                await bad("no hay nada que confirmar en esa foto")
                return None
            ev_ref = _as_uuid(mid)
            ts.ui_intent = policy.DIAGNOSTIC
            if props:  # the question is answered: update the annotated photo in place
                cid = cid or ub.stable_id(ts.session_id, mid)
                ts.ui_pending.append(ub.ui_component("image_annotated", {**props, "question": None, "confirm_fact": None}, "",
                                                     "Foto revisada.", id=cid))  # fmt: skip
            if payload.get("confirmed"):
                for k, v in found:
                    await self._ui_fact(sid, ts, k, v, 1.0, "photo", f"{why}: confirmó lo visto en la foto", ev_ref)
                return "Sí, así está la luz."
            for k, _v in found:
                if "desconocida" in (rs.facts.get(k) or {}).get("values", []):
                    await self._ui_fact(
                        sid, ts, k, "desconocida", 1.0, "user_reported", f"{why}: descartó lo visto en la foto"
                    )
                ts.ui_force = next(
                    (a for a, v in rs.actions.items() if v.get("kind") == "ask" and v.get("fact") == k), None
                )
            return "No, la foto no muestra eso."

        if action == "gauge_retry":
            return "Repita la prueba, por favor."
        await self._trace(sid, ts, "percibir", reason=f"{why}: abrió {payload.get('kind') or 'el informe'}")
        return ""  # report_open: metric only

    async def _media(self, svc: "ConversationService", media_id: str, session_id: str) -> dict | None:
        getter = getattr(svc.repo, "get_media", None)
        if getter is None or not media_id:
            return None
        try:
            return await getter(media_id, session_id)
        except Exception as e:
            log.warning("media lookup failed: %s", type(e).__name__)
            return None

    async def perceive_media(self, svc: "ConversationService", ts: "TurnState", media: dict | None, typed: str) -> str:
        """Image -> facts from the analysis (source 'photo', certainty 0.6) + image_annotated asking to confirm.
        Missing media/analysis -> the turn continues with what the person wrote, telling the LLM why."""
        sid = UUID(ts.session_id)
        if media is None:
            ts.ui_note = "INSTRUCCIÓN (no la menciones): La persona adjuntó un archivo que no se pudo abrir; pídale en media frase que describa lo que ve."
            return typed or "Le envié una foto."
        analysis = _analysis(media)
        found, _ = ub.facts_from_analysis(analysis)
        if analysis:
            url = f"/api/media/{media['id']}?session_id={ts.session_id}"
            ts.ui_pending.append(ub.image_annotated(media, url, analysis, id=ub.stable_id(ts.session_id, media["id"])))
        for k, v in found:
            await self._ui_fact(
                sid,
                ts,
                k,
                v,
                0.6,
                "photo",
                "foto analizada (certeza 0.6: falta que la persona confirme)",
                _as_uuid(media["id"]),
            )
        screen = (analysis or {}).get("screen_text")
        if screen and not found:  # screenshot of ipconfig/ping/an error page: read it as pasted command output
            ts.ui_note = ("INSTRUCCIÓN (no la menciones): La persona envió una captura de pantalla; su texto va en el "
                          "mensaje. Analícelo como salida de comandos, no hable de luces del módem.")  # fmt: skip
            return f"{typed}\n{screen}".strip()
        if found:
            ts.ui_intent = policy.DIAGNOSTIC
        elif not analysis:
            ts.ui_note = ("INSTRUCCIÓN (no la menciones): La persona envió una foto, pero ahora no se puede analizar "
                          "(no hay un modelo de visión activo). Dígaselo en media frase.")  # fmt: skip
        else:
            ts.ui_note = "INSTRUCCIÓN (no la menciones): En la foto no se distinguen las luces de internet; dígaselo en media frase."
        return typed or "Le envié una foto del módem."


NODE_ACTIONS = {
    "isp": ("ask_luz_modem",),
    "modem": ("ask_luz_modem", "ask_luz_tras_reinicio"),
    "router": ("ask_otros_equipos", "ask_contrasena"),
    "devices": ("ask_otros_equipos",),
    "pc": ("ask_otros_equipos", "ask_contrasena"),
}


def _analysis(media: dict | None) -> dict | None:
    a = (media or {}).get("analysis")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return None
    return a if isinstance(a, dict) else None


def _as_uuid(v: str) -> UUID | None:
    try:
        return UUID(str(v))
    except ValueError:
        return None
