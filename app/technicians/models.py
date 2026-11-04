from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

Level = Literal["n1", "n2"]
Availability = Literal["disponible", "ocupado", "fuera_de_turno", "vacaciones"]
Tag = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")]


class TechnicianBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    email_or_username: str | None = Field(default=None, max_length=200)
    specialties: list[Tag] = Field(default_factory=list)
    zones: list[Tag] = Field(default_factory=list)
    level: Level = "n1"
    max_open_tickets: int = Field(default=5, ge=1, le=100)
    availability: Availability = "disponible"
    schedule: dict[str, list[str]] = Field(default_factory=lambda: {"lun-vie": ["08:00", "17:00"]})
    contact_channel: str | None = Field(default=None, max_length=200)


class TechnicianCreate(TechnicianBase):
    pass


class TechnicianPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email_or_username: str | None = Field(default=None, max_length=200)
    specialties: list[Tag] | None = None
    zones: list[Tag] | None = None
    level: Level | None = None
    max_open_tickets: int | None = Field(default=None, ge=1, le=100)
    availability: Availability | None = None
    schedule: dict[str, list[str]] | None = None
    contact_channel: str | None = Field(default=None, max_length=200)
    active: bool | None = None


class Technician(TechnicianBase):
    id: UUID
    active: bool
    data_origin: str
    open_tickets: int = 0
    created_at: datetime
    updated_at: datetime | None = None

    @computed_field  # alias of `active` for the frontend
    @property
    def is_active(self) -> bool:
        return self.active


class Workload(BaseModel):
    technician_id: UUID
    open_tickets: int
    max_open_tickets: int
    by_status: dict[str, int]
    next_sla_due_at: datetime | None = None
    tickets: list[str]
