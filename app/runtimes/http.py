"""Shared httpx plumbing for adapters."""

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import ChatDelta, RuntimeCallError, UnsupportedError


def make_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=float(os.environ.get("RUNTIME_CONNECT_TIMEOUT_S", "10")),
        read=float(os.environ.get("RUNTIME_READ_TIMEOUT_S", "300")),  # model loads can be slow
        write=30.0,
        pool=10.0,
    )


HEALTH_TIMEOUT = httpx.Timeout(2.0)


def pick(params: dict[str, Any] | None, mapping: dict[str, str]) -> dict[str, Any]:
    """Map profile params -> request fields; unknown params and None values are ignored."""
    p = params or {}
    return {dst: p[src] for src, dst in mapping.items() if p.get(src) is not None}


class BaseAdapter:
    kind = "base"
    supports_management = False

    def __init__(self, base_url: str, token: str | None = None, client: httpx.AsyncClient | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}  # never logged
        self._c = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=make_timeout())

    async def aclose(self) -> None:
        await self._c.aclose()

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._c.request(method, path, **kw)
            r.raise_for_status()
            return r.json() if r.content else None
        except httpx.HTTPStatusError as e:
            raise RuntimeCallError(
                f"{self.kind} {method} {path} -> {e.response.status_code}", status=e.response.status_code
            ) from e
        except httpx.HTTPError as e:
            raise RuntimeCallError(f"{self.kind} unreachable: {type(e).__name__}") from e

    async def _ok(self, path: str) -> bool:
        try:
            r = await self._c.get(path, timeout=HEALTH_TIMEOUT)
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    async def _lines(self, method: str, path: str, **kw: Any) -> AsyncIterator[str]:
        try:
            async with self._c.stream(method, path, **kw) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise RuntimeCallError(f"{self.kind} {method} {path} -> {r.status_code}")
                async for line in r.aiter_lines():
                    if line.strip():
                        yield line
        except httpx.HTTPError as e:
            raise RuntimeCallError(f"{self.kind} unreachable: {type(e).__name__}") from e

    async def _bytes_stream(self, method: str, path: str, **kw: Any) -> AsyncIterator[bytes]:
        try:
            async with self._c.stream(method, path, **kw) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise RuntimeCallError(f"{self.kind} {method} {path} -> {r.status_code}")
                async for chunk in r.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as e:
            raise RuntimeCallError(f"{self.kind} unreachable: {type(e).__name__}") from e

    # --- defaults: capabilities a kind does not have
    async def list_loaded(self):  # type: ignore[no-untyped-def]
        return None

    async def load(self, profile) -> None:  # type: ignore[no-untyped-def]
        return None

    async def unload(self, model_ref: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def _chat_stream(self, *a: Any, **k: Any) -> AsyncIterator[ChatDelta]:
        raise UnsupportedError(f"{self.kind} does not support chat")
        yield ChatDelta()  # pragma: no cover

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatDelta]:
        gen = self._chat_stream(model, messages, tools, params)
        if stream:
            async for d in gen:
                yield d
            return
        agg = ChatDelta(done=True)
        async for d in gen:
            agg.content += d.content
            agg.thinking += d.thinking
            agg.tool_calls += d.tool_calls
            agg.finish_reason = d.finish_reason or agg.finish_reason
        yield agg

    async def embed(self, model: str, texts: list[str], params: dict[str, Any] | None = None) -> list[list[float]]:
        raise UnsupportedError(f"{self.kind} does not support embeddings")

    async def transcribe(self, model, audio, mime, language=None, params=None) -> str:  # type: ignore[no-untyped-def]
        raise UnsupportedError(f"{self.kind} does not support transcription")

    async def synthesize(self, model, text, voice=None, format="wav", params=None) -> bytes:  # type: ignore[no-untyped-def]
        return b"".join([c async for c in self.synthesize_stream(model, text, voice, format, params)])

    async def synthesize_stream(self, model, text, voice=None, format="wav", params=None):  # type: ignore[no-untyped-def]
        raise UnsupportedError(f"{self.kind} does not support speech synthesis")
        yield b""  # pragma: no cover

    async def pull(self, model_ref: str) -> AsyncIterator[dict[str, Any]]:
        raise UnsupportedError(f"{self.kind} does not support pulling models")
        yield {}  # pragma: no cover


def loads(line: str) -> dict[str, Any]:
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise RuntimeCallError("invalid JSON from runtime") from e
