from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)
    value: Any
    source: str = "user_reported"
    certainty: float = Field(default=1.0, ge=0, le=1)


class RuleContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_yaml: str
