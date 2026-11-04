"""Ollama. API: https://github.com/ollama/ollama/blob/main/docs/api.md"""

from collections.abc import AsyncIterator
from typing import Any

from app.config.models import ModelProfile

from .base import ChatDelta, LoadedModel, RuntimeCallError, ToolCall
from .http import BaseAdapter, loads, pick

_OPTIONS = {
    "temperature": "temperature",
    "top_k": "top_k",
    "top_p": "top_p",
    "repeat_penalty": "repeat_penalty",
    "ctx": "num_ctx",
    "max_tokens": "num_predict",
    "seed": "seed",
}


def _mb(n: int | None) -> int | None:
    return round(n / 1024 / 1024) if n else None


class OllamaAdapter(BaseAdapter):
    kind = "ollama"
    supports_management = True

    async def health(self) -> bool:
        return await self._ok("/api/tags")

    async def list_loaded(self) -> list[LoadedModel] | None:
        data = await self._json("GET", "/api/ps")
        return [LoadedModel(m.get("model") or m["name"], _mb(m.get("size"))) for m in (data or {}).get("models", [])]

    async def list_installed(self) -> list[str]:
        data = await self._json("GET", "/api/tags")
        return [m["name"] for m in (data or {}).get("models", [])]

    async def _keep(self, model: str, role: str | None, keep_alive: Any, options: dict[str, Any] | None = None) -> None:
        # embedding models are loaded through /api/embed, everything else through /api/generate (empty prompt)
        if role == "embedding":
            await self._json("POST", "/api/embed", json={"model": model, "input": "", "keep_alive": keep_alive})
        else:
            body: dict[str, Any] = {"model": model, "prompt": "", "stream": False, "keep_alive": keep_alive}
            if options:
                body["options"] = options
            await self._json("POST", "/api/generate", json=body)

    async def load(self, profile: ModelProfile) -> None:
        keep = profile.params.get("keep_alive", -1)  # the manager owns eviction, not Ollama's TTL
        await self._keep(profile.model_ref, profile.role, keep, pick(profile.params, {"ctx": "num_ctx"}))

    async def unload(self, model_ref: str, params: dict[str, Any] | None = None) -> None:
        try:
            await self._keep(model_ref, None, 0)
        except RuntimeCallError:
            await self._keep(model_ref, "embedding", 0)

    async def _chat_stream(self, model, messages, tools, params) -> AsyncIterator[ChatDelta]:  # type: ignore[no-untyped-def]
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        if opts := pick(params, _OPTIONS):
            body["options"] = opts
        if (p := params or {}).get("keep_alive") is not None:
            body["keep_alive"] = p["keep_alive"]
        async for line in self._lines("POST", "/api/chat", json=body):
            d = loads(line)
            if "error" in d:
                raise RuntimeCallError(f"ollama: {d['error']}")
            m = d.get("message") or {}
            calls = [
                ToolCall(name=c["function"]["name"], arguments=c["function"].get("arguments") or {}, id=c.get("id"))
                for c in m.get("tool_calls") or []
            ]
            yield ChatDelta(
                content=m.get("content", ""),
                thinking=m.get("thinking", ""),
                tool_calls=calls,
                done=bool(d.get("done")),
                finish_reason=d.get("done_reason") if d.get("done") else None,
                usage=(
                    {
                        "prompt_tokens": d.get("prompt_eval_count") or 0,
                        "cached_tokens": 0,
                        "completion_tokens": d.get("eval_count") or 0,
                    }
                    if d.get("done")
                    else None
                ),
            )

    async def embed(self, model: str, texts: list[str], params: dict[str, Any] | None = None) -> list[list[float]]:
        body: dict[str, Any] = {"model": model, "input": texts, **pick(params, {"dimension": "dimensions"})}
        if (params or {}).get("keep_alive") is not None:
            body["keep_alive"] = params["keep_alive"]  # type: ignore[index]
        data = await self._json("POST", "/api/embed", json=body)
        return data["embeddings"]

    async def pull(self, model_ref: str) -> AsyncIterator[dict[str, Any]]:
        async for line in self._lines("POST", "/api/pull", json={"model": model_ref, "stream": True}):
            d = loads(line)
            if "error" in d:
                raise RuntimeCallError(f"ollama: {d['error']}")
            yield d
