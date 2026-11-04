"""Runtime adapter port + shared types. One adapter per provider kind (see factory.py)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config.models import ModelProfile


class RuntimeCallError(Exception):
    """Runtime unreachable / returned an error. `status` is the HTTP status when the runtime answered."""

    def __init__(self, msg: str = "", status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


class UnsupportedError(RuntimeCallError):
    """The provider kind does not support this capability."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass
class ChatDelta:
    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    done: bool = False
    finish_reason: str | None = None  # "stop" | "length" | "tool_calls" ... (last delta)
    usage: dict[str, int] | None = None  # last delta: prompt_tokens / cached_tokens / completion_tokens


@dataclass(frozen=True)
class LoadedModel:
    model_ref: str
    memory_mb: int | None = None


class RuntimeAdapter(Protocol):
    kind: str
    supports_management: bool  # False -> load/unload are no-ops (e.g. llama.cpp outside router mode)

    async def health(self) -> bool: ...
    async def list_loaded(self) -> list[LoadedModel] | None:
        """None = runtime cannot tell (manager keeps its own bookkeeping)."""
        ...

    async def load(self, profile: ModelProfile) -> None: ...
    async def unload(self, model_ref: str, params: dict[str, Any] | None = None) -> None: ...
    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatDelta]: ...
    async def embed(self, model: str, texts: list[str], params: dict[str, Any] | None = None) -> list[list[float]]: ...
    async def transcribe(
        self, model: str, audio: bytes, mime: str, language: str | None = None, params: dict[str, Any] | None = None
    ) -> str: ...
    async def synthesize(
        self, model: str, text: str, voice: str | None = None, format: str = "wav", params: dict[str, Any] | None = None
    ) -> bytes: ...
    def synthesize_stream(
        self, model: str, text: str, voice: str | None = None, format: str = "wav", params: dict[str, Any] | None = None
    ) -> AsyncIterator[bytes]: ...
    def pull(self, model_ref: str) -> AsyncIterator[dict[str, Any]]: ...
    async def aclose(self) -> None: ...
