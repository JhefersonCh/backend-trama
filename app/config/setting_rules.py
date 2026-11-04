from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .errors import DomainValidationError


def _no_bool_or_str(v: Any) -> Any:
    if isinstance(v, bool | str):
        raise ValueError("must be a number")
    return v


Num01 = Annotated[float, BeforeValidator(_no_bool_or_str), Field(ge=0, le=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WakeWord(_Strict):
    enabled: StrictBool
    phrase: Annotated[StrictStr, Field(min_length=1, max_length=40, pattern=r"\S")]


class Vad(_Strict):
    positive_threshold: Num01
    min_silence_ms: Annotated[StrictInt, Field(ge=0)]


AUTONOMY_LEVELS = ("autonomo", "con_aviso", "con_confirmacion")
AutonomyLevel = Literal["autonomo", "con_aviso", "con_confirmacion"]


class CfThresholds(_Strict):
    accept: Annotated[float, BeforeValidator(_no_bool_or_str), Field(ge=-1, le=1)]
    explore_min: Annotated[float, BeforeValidator(_no_bool_or_str), Field(ge=-1, le=1)]
    reject: Annotated[float, BeforeValidator(_no_bool_or_str), Field(ge=-1, le=1)]

    @model_validator(mode="after")
    def _ordered(self) -> "CfThresholds":
        if not self.reject < self.explore_min < self.accept:
            raise ValueError("must satisfy reject < explore_min < accept")
        return self


class AutonomyPolicy(_Strict):
    ticket_create_on_request: AutonomyLevel
    ticket_create_own_initiative: AutonomyLevel
    assign_technician: AutonomyLevel
    renew_ip: AutonomyLevel
    share_contact: AutonomyLevel
    close_case: AutonomyLevel


class FollowupHours(_Strict):
    after_ticket: Annotated[float, BeforeValidator(_no_bool_or_str), Field(gt=0)]
    after_step: Annotated[float, BeforeValidator(_no_bool_or_str), Field(gt=0)]


_Hours = Annotated[StrictInt, Field(gt=0)]


class SlaHours(_Strict):
    critical: _Hours
    high: _Hours
    medium: _Hours
    low: _Hours


_Tag = Annotated[StrictStr, Field(pattern=r"^[a-z0-9_]+$", max_length=40)]


def _ordered_subset(v: Any) -> Any:
    allowed = ["zona", "modalidad", "franja", "contacto"]
    if not isinstance(v, list) or any(x not in allowed for x in v) or len(set(v)) != len(v):
        raise ValueError(f"must be a list without repeats drawn from {allowed}")
    if v != [x for x in allowed if x in v]:
        raise ValueError(f"must keep the order {allowed}")
    return v


class TokenBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[StrictInt, Field(ge=16, le=8000)]
    voice: Annotated[StrictInt, Field(ge=16, le=8000)]


# Answer length limits per turn type (LLM max_tokens). Missing keys fall back to the code defaults.
TOKEN_INTENTS = Literal[
    "diagnostic", "guide", "output", "clarify", "consulta", "ticket", "inventory",
    "smalltalk", "closing", "plantilla", "extraccion",
]  # fmt: skip

RULES: dict[str, TypeAdapter] = {
    "max_tokens_by_intent": TypeAdapter(dict[TOKEN_INTENTS, TokenBudget]),
    "ram_budget_mb": TypeAdapter(Annotated[StrictInt, Field(ge=512, le=65536)]),
    "strict_single_model": TypeAdapter(StrictBool),
    "barge_in_min_ms": TypeAdapter(Annotated[StrictInt, Field(ge=50, le=3000)]),
    "rag_top_k": TypeAdapter(Annotated[StrictInt, Field(ge=1, le=50)]),
    "wake_word": TypeAdapter(WakeWord),
    "vad": TypeAdapter(Vad),
    "system_prompt": TypeAdapter(Annotated[StrictStr, Field(pattern=r"\S")]),
    # auto: backend retrieves every turn and injects context; tool: LLM calls search_knowledge; both: the two
    "rag_mode": TypeAdapter(Literal["auto", "tool", "both"]),
    "rag_min_score": TypeAdapter(Num01),
    # hybrid ranking: cosine x per-document_type factor (+ lexical bonus). Absent/{} = pure dense ranking.
    "rag_type_weights": TypeAdapter(
        dict[Annotated[StrictStr, Field(min_length=1, max_length=40)], Annotated[float, Field(ge=0.5, le=1.5)]]
    ),
    "rag_lexical_weight": TypeAdapter(Num01),
    # answer depth: auto = detailed but friendly, more technical once the user writes technically
    "audience_level": TypeAdapter(Literal["auto", "basico", "tecnico"]),
    "trato": TypeAdapter(Literal["usted", "tu"]),
    # background load of the active llm + embedding profiles after startup (avoids the cold-start delay)
    "warmup_on_start": TypeAdapter(StrictBool),
    # uploaded photos/audio older than this are deleted (0 = keep forever)
    "media_retention_days": TypeAdapter(Annotated[StrictInt, Field(ge=0, le=3650)]),
    # agent core (Fase 0)
    "cf_thresholds": TypeAdapter(CfThresholds),
    "autonomy_policy": TypeAdapter(AutonomyPolicy),
    "followup_hours": TypeAdapter(FollowupHours),
    # assignment (Fase 1)
    "assignment_strategy": TypeAdapter(Literal["menor_carga", "round_robin", "manual"]),
    "sla_hours": TypeAdapter(SlaHours),
    "assignment_questions": TypeAdapter(Annotated[list[StrictStr], BeforeValidator(_ordered_subset)]),
    "zones": TypeAdapter(Annotated[list[_Tag], Field(min_length=1, max_length=100)]),
}


def validate_setting(key: str, value: Any) -> Any:
    rule = RULES.get(key)
    if rule is None:
        raise DomainValidationError(f"unknown setting key: {key}")
    try:
        parsed = rule.validate_python(value)
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(map(str, x['loc'])) or key}: {x['msg']}" for x in e.errors())
        raise DomainValidationError(f"invalid value for {key}: {msgs}") from e
    return rule.dump_python(parsed, mode="json")
