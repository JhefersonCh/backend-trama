"""Closed catalog of generative-UI components (contract §2). The backend validates props BEFORE emitting:
an invalid component is a bug in a builder, never something the LLM produced."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ quick_replies
class Option(_P):
    label: str = Field(min_length=1, max_length=80)
    value: Any = None


class QuickReplies(_P):
    fact: str
    question: str
    options: list[Option] = Field(min_length=1, max_length=8)
    allow_free_text: bool = True


# ------------------------------------------------------------------ checklist
class Step(_P):
    id: str
    text: str
    status: Literal["pending", "done", "failed"] = "pending"


class Checklist(_P):
    action_id: str
    title: str
    steps: list[Step] = Field(min_length=1, max_length=12)
    current: str | None = None


# ------------------------------------------------------------------ form
class FormField(_P):
    name: str
    label: str
    type: Literal["select", "text", "consent_text"]
    options: list[Option] | None = None
    placeholder: str | None = None
    value: Any = None


class Form(_P):
    form_id: Literal["assignment"] = "assignment"
    title: str
    fields: list[FormField] = Field(min_length=1)
    submit_label: str


# ------------------------------------------------------------------ case_card
class TopHypothesis(_P):
    key: str
    label: str
    cf: float
    confidence_label: str


class CardTicket(_P):
    number: str
    status: str
    status_label: str
    technician: str | None = None
    sla_due_at: str | None = None


class CaseCard(_P):
    state: str
    state_label: str
    top_hypothesis: TopHypothesis | None = None
    next_step: str | None = None
    ticket: CardTicket | None = None


# ------------------------------------------------------------------ hypothesis_bars
class Bar(_P):
    key: str
    label: str
    cf: float
    status: str
    confidence_label: str


class Thresholds(_P):
    accept: float
    reject: float


class HypothesisBars(_P):
    items: list[Bar]
    thresholds: Thresholds


# ------------------------------------------------------------------ timeline
class TimelineItem(_P):
    at: str
    kind: Literal["fact", "step", "ticket", "note", "followup", "system"]
    title: str
    detail: str | None = None


class Timeline(_P):
    items: list[TimelineItem]


# ------------------------------------------------------------------ topology
class TopoNode(_P):
    id: str
    kind: Literal["isp", "modem", "router", "device", "pc"]
    label: str
    status: Literal["ok", "fail", "warn", "unknown"]
    detail: str | None = None


class TopoEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    status: Literal["ok", "fail", "unknown"]


class Topology(_P):
    nodes: list[TopoNode]
    edges: list[TopoEdge]


# ------------------------------------------------------------------ diagnosis_flow
class FlowNode(_P):
    id: str
    kind: Literal["question", "answer", "rule", "hypothesis", "action"]
    label: str
    detail: str | None = None


class FlowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    label: str | None = None


class DiagnosisFlow(_P):
    nodes: list[FlowNode]
    edges: list[FlowEdge]


# ------------------------------------------------------------------ compare_cards
class CompareOption(_P):
    id: str
    title: str
    summary: str
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)


class CompareCards(_P):
    question: str
    options: list[CompareOption] = Field(min_length=2, max_length=4)


# ------------------------------------------------------------------ gauge
class Band(_P):
    to: float
    tone: Literal["bad", "warn", "good"]


class Gauge(_P):
    label: str
    value: float
    unit: str
    max: float
    bands: list[Band]


# ------------------------------------------------------------------ image_annotated
class Box(_P):
    bbox: list[float] = Field(min_length=4, max_length=4)
    label: str
    color: Literal["roja", "verde", "naranja", "apagada", "desconocido"]


class ConfirmFact(_P):
    key: str
    value: Any


class ImageAnnotated(_P):
    media_id: str
    url: str
    width: int | None = None
    height: int | None = None
    boxes: list[Box] = Field(default_factory=list)
    question: str | None = None
    confirm_fact: ConfirmFact | None = None


# ------------------------------------------------------------------ report_card
class ReportCard(_P):
    title: str
    html_url: str
    pdf_url: str | None = None
    kind: Literal["informe", "guia"] = "informe"


COMPONENTS: dict[str, type[BaseModel]] = {
    "quick_replies": QuickReplies,
    "checklist": Checklist,
    "form": Form,
    "case_card": CaseCard,
    "hypothesis_bars": HypothesisBars,
    "timeline": Timeline,
    "topology": Topology,
    "diagnosis_flow": DiagnosisFlow,
    "compare_cards": CompareCards,
    "gauge": Gauge,
    "image_annotated": ImageAnnotated,
    "report_card": ReportCard,
}

# ui_event.action accepted per component (contract §2)
EVENT_ACTIONS: dict[str, tuple[str, ...]] = {
    "quick_replies": ("quick_reply",),
    "checklist": ("step_done", "step_failed"),
    "form": ("form_submit",),
    "case_card": ("case_action",),
    "hypothesis_bars": ("why",),
    "topology": ("topology_node",),
    "compare_cards": ("compare_choose",),
    "gauge": ("gauge_retry",),
    "image_annotated": ("image_confirm",),
    "report_card": ("report_open",),
}
