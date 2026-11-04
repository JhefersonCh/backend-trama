"""Case lifecycle states and transitions (spec §3.5). Pure helper; wired into the turn in PART 2."""

CASE_STATES = (
    "nuevo", "entendiendo", "diagnosticando", "intentando_solucion", "verificando",
    "resuelto", "escalado", "seguimiento", "cerrado",
)  # fmt: skip

# (from_state, event) -> to_state. Event "*" rows apply from any state.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("nuevo", "problem_reported"): "entendiendo",
    ("entendiendo", "hypothesis_found"): "diagnosticando",
    ("diagnosticando", "guided_step_may_resolve"): "intentando_solucion",
    ("intentando_solucion", "verification_requested"): "verificando",
    ("verificando", "verified_working"): "resuelto",
    ("verificando", "still_failing"): "diagnosticando",
    ("resuelto", "followup_ok"): "cerrado",
    ("escalado", "followup_ok"): "cerrado",
    ("seguimiento", "followup_ok"): "cerrado",
    ("escalado", "ticket_resolved"): "seguimiento",
}
ANY_STATE_EVENTS: dict[str, str] = {"escalated": "escalado"}


def next_state(state: str, event: str) -> str:
    """New state for the event, or the same state when the event does not apply."""
    if event in ANY_STATE_EVENTS and state in CASE_STATES:
        return ANY_STATE_EVENTS[event]
    return TRANSITIONS.get((state, event), state)
