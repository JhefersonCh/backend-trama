"""OpenAI-compatible family: llama.cpp server, speaches (voice_service), generic openai_compatible."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config.models import ModelProfile

from .base import ChatDelta, LoadedModel, RuntimeCallError, ToolCall
from .http import BaseAdapter, pick

_CHAT = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",  # llama.cpp extension; ignored by strict OpenAI servers
    "repeat_penalty": "repeat_penalty",  # llama.cpp extension
    "max_tokens": "max_tokens",
    "seed": "seed",
}


log = logging.getLogger("trama.runtime")


class OpenAICompatAdapter(BaseAdapter):
    """Plain OpenAI-style server. No model management."""

    kind = "openai_compatible"
    send_dimensions = True
    health_path = "/v1/models"

    async def health(self) -> bool:
        return await self._ok(self.health_path)

    async def _chat_stream(self, model, messages, tools, params) -> AsyncIterator[ChatDelta]:  # type: ignore[no-untyped-def]
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True, **pick(params, _CHAT)}
        if tools:
            body["tools"] = tools
        if self.kind == "llamacpp":
            body["cache_prompt"] = True  # llama.cpp extension: reuse the KV cache of the common prompt prefix
        else:
            body["stream_options"] = {"include_usage": True}  # OpenAI-style token usage in the last chunk
        # Provider-specific options configured in the profile (UI -> DB), e.g. DeepSeek
        # {"extra_body": {"thinking": {"type": "disabled"}}}. Merged last; cannot override model/messages/stream.
        extra = (params or {}).get("extra_body")
        if isinstance(extra, dict):
            body.update({k: v for k, v in extra.items() if k not in ("model", "messages", "stream", "tools")})
        acc: dict[int, dict[str, Any]] = {}  # tool-call fragments by index
        finish: str | None = None
        usage: dict[str, int] | None = None
        think_filter = ThinkFilter()
        async for line in self._lines("POST", "/v1/chat/completions", json=body):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            d = json.loads(payload)
            if "error" in d:
                raise RuntimeCallError(f"{self.kind}: {d['error']}")
            if (u := d.get("usage")) and not d.get("timings"):  # OpenAI-compatible APIs (stream_options.include_usage)
                details = u.get("prompt_tokens_details") or {}
                usage = {
                    "prompt_tokens": u.get("prompt_tokens") or 0,
                    "cached_tokens": details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0,
                    "completion_tokens": u.get("completion_tokens") or 0,
                }
            if t := d.get("timings"):  # llama.cpp: prompt-cache effectiveness per call
                cached, fresh = t.get("cache_n") or 0, t.get("prompt_n") or 0
                usage = {
                    "prompt_tokens": cached + fresh,
                    "cached_tokens": cached,
                    "completion_tokens": t.get("predicted_n") or 0,
                }
                log.info(
                    "llm timings: cache_n=%s prompt_n=%s prompt_ms=%s predicted_n=%s",
                    t.get("cache_n"),
                    t.get("prompt_n"),
                    round(t.get("prompt_ms") or 0),
                    t.get("predicted_n"),
                )
            choice = (d.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                slot = acc.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["args"] += fn.get("arguments") or ""
            text = think_filter.feed(delta.get("content") or "")
            think = delta.get("reasoning_content") or ""
            if text or think:
                yield ChatDelta(content=text, thinking=think)
        if rest := think_filter.flush():
            yield ChatDelta(content=rest)
        calls = [ToolCall(name=s["name"], arguments=_args(s["args"]), id=s["id"]) for _, s in sorted(acc.items())]
        yield ChatDelta(tool_calls=calls, done=True, finish_reason=finish, usage=usage)

    async def embed(self, model: str, texts: list[str], params: dict[str, Any] | None = None) -> list[list[float]]:
        body: dict[str, Any] = {"model": model, "input": texts}
        if self.send_dimensions:
            body.update(pick(params, {"dimension": "dimensions"}))
        data = await self._json("POST", "/v1/embeddings", json=body)
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]

    async def transcribe(self, model, audio, mime, language=None, params=None) -> str:  # type: ignore[no-untyped-def]
        form = {"model": model, "response_format": "json"}
        if language := language or (params or {}).get("language"):
            form["language"] = language
        if prompt := (params or {}).get("prompt"):
            form["prompt"] = str(prompt)  # vocabulary hint: faster-whisper uses it as initial_prompt
        ext = mime.split("/")[-1].split(";")[0] or "wav"
        data = await self._json(
            "POST", "/v1/audio/transcriptions", data=form, files={"file": (f"audio.{ext}", audio, mime)}
        )
        return data["text"]

    async def synthesize_stream(self, model, text, voice=None, format="wav", params=None):  # type: ignore[no-untyped-def]
        body: dict[str, Any] = {"model": model, "input": text, "response_format": format}
        if voice := voice or (params or {}).get("voice"):
            body["voice"] = voice
        if (params or {}).get("speed") is not None:
            body["speed"] = params["speed"]  # type: ignore[index]
        async for chunk in self._bytes_stream("POST", "/v1/audio/speech", json=body):
            yield chunk


class ThinkFilter:
    """Streaming filter for reasoning leaks in `content` (seen with LFM3 even with reasoning off).

    - `<think>...</think>answer` -> drops the thinking block.
    - `answer</think>answer` (duplicated answer) -> the text after the tag is suppressed while it repeats what was
      already emitted; anything new after that is emitted normally.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.mode = "probe"  # probe -> (in_think) -> pass <-> dedupe
        self.buf = ""
        self.emitted = ""
        self.pending = ""

    def _out(self, s: str) -> str:
        self.emitted += s
        return s

    def feed(self, text: str) -> str:
        if not text:
            return ""
        if self.mode == "in_think":
            self.buf += text
        elif self.mode == "probe":
            self.buf += text
            head = self.buf.lstrip()
            if head.startswith(self.OPEN):
                self.mode = "in_think"
            elif len(head) >= len(self.OPEN) or not self.OPEN.startswith(head):
                text, self.buf, self.mode = self.buf, "", "pass"
            else:
                return ""  # could still become "<think>"
        if self.mode == "in_think":
            if self.CLOSE not in self.buf:
                return ""
            text, self.buf, self.mode = self.buf.split(self.CLOSE, 1)[1].lstrip(), "", "pass"
        if self.mode == "pass":
            if self.CLOSE not in text:
                return self._out(text)
            before, after = text.split(self.CLOSE, 1)
            out = self._out(before)
            self.mode, self.pending = "dedupe", ""
            return out + self.feed(after) if after else out
        # dedupe: hold text while it is a prefix of what was already emitted
        self.pending += text.replace(self.CLOSE, "")
        cand, ref = self.pending.lstrip(), self.emitted.strip()
        if ref.startswith(cand.rstrip()) or not cand:
            if cand.strip() == ref:  # full duplicate consumed
                self.mode, self.pending = "pass", ""
            return ""
        if ref and cand.startswith(ref):  # duplicate followed by new text: keep only the new part
            out, self.mode, self.pending = cand[len(ref) :], "pass", ""
            return self._out(out)
        out, self.mode, self.pending = self.pending, "pass", ""
        return self._out(out)

    def flush(self) -> str:
        if self.mode == "probe":
            out, self.buf = self.buf, ""
            return self._out(out)
        if self.mode == "in_think":  # never closed: treat as plain text without the tag
            out, self.buf = self.buf.replace(self.OPEN, "").lstrip(), ""
            return self._out(out)
        return ""  # dedupe leftovers are a (partial) duplicate


def _args(raw: str) -> dict[str, Any]:
    try:
        v = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"_raw": raw}
    return v if isinstance(v, dict) else {"_value": v}


class LlamaCppAdapter(OpenAICompatAdapter):
    """llama.cpp llama-server. Router mode: GET /models, POST /models/load|unload {"model": id}.

    Docs: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md (section "Using multiple models").
    Outside router mode GET /models is absent -> `supports_management` stays False and load/unload are no-ops.
    """

    kind = "llamacpp"
    send_dimensions = False
    health_path = "/health"

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self._router: bool | None = None

    @property
    def supports_management(self) -> bool:  # type: ignore[override]
        return bool(self._router)

    async def _models(self) -> list[dict[str, Any]] | None:
        try:
            data = await self._json("GET", "/models")
        except RuntimeCallError:
            return None
        rows = data.get("data") if isinstance(data, dict) else None
        if rows is None or not all(isinstance(r.get("status"), dict) for r in rows):
            return None  # single-model server: /models absent or not router-shaped
        return rows

    async def _detect(self) -> list[dict[str, Any]] | None:
        rows = await self._models()
        self._router = rows is not None
        return rows

    async def list_loaded(self) -> list[LoadedModel] | None:
        rows = await self._detect()
        if rows is None:
            return None
        return [LoadedModel(r["id"]) for r in rows if r["status"].get("value") == "loaded"]

    @staticmethod
    def _id(model_ref: str, params: dict[str, Any] | None) -> str:
        return (params or {}).get("router_model") or model_ref

    LOAD_TIMEOUT_S = 900  # first load may download the GGUF from Hugging Face
    POLL_S = 1.0

    async def load(self, profile: ModelProfile) -> None:
        if self._router is None:
            await self._detect()
        if not self._router:
            return
        mid = self._id(profile.model_ref, profile.params)
        await self._json("POST", "/models/load", json={"model": mid})
        # Router load is async: poll until the model reports "loaded".
        deadline = time.monotonic() + self.LOAD_TIMEOUT_S
        seen_busy = False
        while time.monotonic() < deadline:
            rows = await self._models() or []
            state = next((r["status"].get("value") for r in rows if r.get("id") == mid), None)
            if state == "loaded":
                return
            if state in ("loading", "downloading"):
                seen_busy = True
            elif state in ("unloaded", "incomplete", None) and seen_busy:
                raise RuntimeCallError(f"llamacpp: model {mid} failed to load (state={state})")
            await asyncio.sleep(self.POLL_S)
        raise RuntimeCallError(f"llamacpp: timeout loading {mid}")

    async def unload(self, model_ref: str, params: dict[str, Any] | None = None) -> None:
        if self._router is None:
            await self._detect()
        if self._router:
            await self._json("POST", "/models/unload", json={"model": self._id(model_ref, params)})


class VoiceServiceAdapter(OpenAICompatAdapter):
    """speaches. Loaded-model management via /api/ps (list), POST/DELETE /api/ps/{model_id} (load/unload).

    Docs: https://speaches.ai (the /api/ps routes mirror Ollama's; if absent we fall back to speaches' own TTL).
    """

    kind = "voice_service"
    supports_management = True
    health_path = "/health"

    async def list_loaded(self) -> list[LoadedModel] | None:
        try:
            data = await self._json("GET", "/api/ps")
        except RuntimeCallError:
            return None
        items = data.get("models", data) if isinstance(data, dict) else data
        out = []
        for m in items or []:
            ref = m if isinstance(m, str) else (m.get("model") or m.get("id") or m.get("name"))
            if ref:
                out.append(LoadedModel(ref))
        return out

    DOWNLOAD_TIMEOUT = httpx.Timeout(900.0, connect=5.0)

    async def load(self, profile: ModelProfile) -> None:
        mid = profile.model_ref
        try:
            await self._json("POST", f"/api/ps/{mid}")
            return
        except RuntimeCallError as e:
            if e.status == 409:  # already loaded
                return
            if e.status is None:
                raise
        # speaches does not auto-download: fetch the model to disk (blocking, 1-3 min), then load it.
        await self._json("POST", f"/v1/models/{mid}", timeout=self.DOWNLOAD_TIMEOUT)
        try:
            await self._json("POST", f"/api/ps/{mid}")
        except RuntimeCallError as e:
            if e.status != 409:
                raise

    async def unload(self, model_ref: str, params: dict[str, Any] | None = None) -> None:
        try:
            await self._json("DELETE", f"/api/ps/{model_ref}")
        except RuntimeCallError:
            pass  # rely on speaches' TTL
