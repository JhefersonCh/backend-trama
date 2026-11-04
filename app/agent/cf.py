"""Certainty-factor math (MYCIN). Pure."""


def combine(a: float, b: float) -> float:
    if a >= 0 and b >= 0:
        r = a + b * (1 - a)
    elif a < 0 and b < 0:
        r = a + b * (1 + a)
    else:
        r = (a + b) / (1 - min(abs(a), abs(b)))
    return max(-1.0, min(1.0, r))


def weighted(rule_cf: float, fact_certainty: float) -> float:
    """A rule's contribution is scaled by how sure we are of the fact that triggered it."""
    return rule_cf * fact_certainty
