from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class LoadedOut(BaseModel):
    profile_id: UUID | None
    name: str
    role: str | None
    memory_mb: int | None
    source: Literal["manager", "external"]
    last_used_at: datetime | None


class ProviderHealth(BaseModel):
    provider_id: UUID
    name: str
    kind: str
    reachable: bool
    latency_ms: int | None


class RuntimeStatus(BaseModel):
    ram_budget_mb: int | None
    strict_single_model: bool
    used_mb: int
    loaded: list[LoadedOut]
    providers: list[ProviderHealth]


class LoadResult(RuntimeStatus):
    evicted: list[str] = []
    warning: str | None = None


class TestResult(BaseModel):
    ok: bool
    latency_ms: int
    detail: str
    dimension: int | None = None
    measured_memory_mb: int | None = None
    skipped: bool = False


class PullBody(BaseModel):
    model_ref: str
