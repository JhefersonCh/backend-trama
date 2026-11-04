from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Kind = Literal["ollama", "llamacpp", "openai_compatible", "voice_service"]
Role = Literal["llm", "embedding", "stt", "tts", "vision"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Provider(BaseModel):
    id: UUID
    name: str
    kind: Kind
    base_url: str
    enabled: bool
    has_secret: bool
    created_at: datetime
    updated_at: datetime


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    kind: Kind
    base_url: Url
    enabled: bool = True


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    kind: Kind | None = None
    base_url: Url | None = None
    enabled: bool | None = None


class SecretBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1)


class ModelProfile(BaseModel):
    id: UUID
    name: str
    role: Role
    provider_id: UUID
    model_ref: str
    params: dict[str, Any]
    est_memory_mb: int | None
    measured_memory_mb: int | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    role: Role
    provider_id: UUID
    model_ref: Name
    params: dict[str, Any] = Field(default_factory=dict)
    est_memory_mb: int | None = Field(default=None, ge=0)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # is_active is rejected here
    name: Name | None = None
    role: Role | None = None
    provider_id: UUID | None = None
    model_ref: Name | None = None
    params: dict[str, Any] | None = None
    est_memory_mb: int | None = Field(default=None, ge=0)


class ActivationResult(BaseModel):
    profile: ModelProfile
    reindex_required: bool


class SettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any


class SettingOut(BaseModel):
    key: str
    value: Any


class AuditLog(BaseModel):
    id: int
    entity: str
    entity_id: str
    action: str
    old_value: Any = None
    new_value: Any = None
    changed_at: datetime
