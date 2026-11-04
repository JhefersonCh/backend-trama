from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Kind = Literal["builtin", "static_template", "rag_search", "sql_readonly", "http_request", "client_tool"]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=400)]


class Usage(BaseModel):
    calls: int = 0
    errors: int = 0
    last_used_at: datetime | None = None


class Tool(BaseModel):
    id: UUID
    name: str
    kind: Kind
    description: str
    params_schema: dict[str, Any]
    config: dict[str, Any]
    intents: list[str]
    enabled: bool
    version: int
    updated_at: datetime
    usage: Usage = Field(default_factory=Usage)


class ToolCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    kind: Kind
    description: Description
    params_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    config: dict[str, Any] = Field(default_factory=dict)
    intents: list[str] = Field(default_factory=list)
    enabled: bool = True


class ToolPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: Description | None = None
    params_schema: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    intents: list[str] | None = None
    enabled: bool | None = None


class ToolTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)


class ToolTestResult(BaseModel):
    ok: bool
    output: Any = None
    duration_ms: int
    error: str | None = None


class ToolVersion(BaseModel):
    version: int
    snapshot: dict[str, Any]
    created_at: datetime
