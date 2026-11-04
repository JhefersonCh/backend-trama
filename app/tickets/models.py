from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Priority = Literal["low", "medium", "high", "critical"]
TicketStatus = Literal["open", "in_progress", "resolved", "closed"]  # settable via API
Title = Field(min_length=1, max_length=300)


class TicketSummary(BaseModel):
    ticket_number: str
    status: str
    priority: str
    title: str
    created_at: datetime
    updated_at: datetime | None = None
    technician_id: UUID | None = None
    technician_name: str | None = None  # assigned technician, if any
    sla_due_at: datetime | None = None
    assignment_reason: str | None = None  # why this technician (or why queued); only set right after auto-assignment
    queued: bool | None = None  # True when auto-assignment found nobody (ticket waits in the unassigned queue)


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Title
    description: str = Field(default="", max_length=10000)
    category: str | None = Field(default=None, max_length=100)
    priority: Priority = "medium"
    session_id: str | None = None
    zone: str | None = Field(default=None, max_length=60)
    service_mode: Literal["remoto", "visita"] | None = None
    preferred_slot: Literal["manana", "tarde", "noche", "cualquiera"] | None = None
    handoff_summary: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class TicketPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: TicketStatus | None = None
    priority: Priority | None = None


class TicketEvent(BaseModel):
    id: UUID
    event_type: str
    old_value: Any = None
    new_value: Any = None
    details: str | None = None
    created_at: datetime


class TicketDetail(TicketSummary):
    incident_id: UUID
    description: str | None
    category: str | None
    source_type: str
    reporter_ref: str | None
    closed_at: datetime | None
    assigned_technician_id: UUID | None = None
    session_id: UUID | None = None
    zone: str | None = None
    service_mode: str | None = None
    preferred_slot: str | None = None
    handoff_summary: dict[str, Any] | None = None
    has_contact: bool = False  # the contact itself is never returned
    events: list[TicketEvent]


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    technician_id: UUID | None = None
    auto: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> "AssignBody":
        if self.auto == (self.technician_id is not None):
            raise ValueError("send either technician_id or auto=true")
        return self


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    technician_id: UUID
    text: str = Field(min_length=1, max_length=2000)


class ContactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=3, max_length=200)
    consent: Literal[True]


class TimelineTechnician(BaseModel):
    id: UUID
    name: str
    specialties: list[str]


class LastNote(BaseModel):
    text: str
    technician_name: str | None = None
    at: datetime


class TicketTimeline(BaseModel):
    ticket_number: str
    status: str
    priority: str
    title: str
    created_at: datetime
    opened_at: datetime  # same as created_at
    session_id: UUID | None = None
    age_seconds: int
    technician: TimelineTechnician | None
    queued: bool  # no technician and an unassigned_queue event exists
    sla_due_at: datetime | None
    sla_remaining_seconds: int | None  # negative = overdue
    handoff_summary: dict[str, Any] | None
    last_note: LastNote | None
    has_contact: bool
    events: list[TicketEvent]
