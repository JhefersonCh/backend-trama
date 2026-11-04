# ruff: noqa: E501
"""Tool registry: OpenAI-format definitions + validated, time-boxed, audited execution."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from .ports import AuditPort

log = logging.getLogger("trama.tools")

MAX_CONTENT_CHARS = 8000  # what we feed back to the LLM
_SUMMARY_CHARS = 300


@dataclass(frozen=True)
class ToolContext:
    session_id: str | None
    turn_id: str | None = None
    rag_top_k: int = 5


@dataclass
class ToolOutput:
    data: Any
    summary: str
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolResult:
    name: str
    ok: bool
    status: str  # ok | error | denied | timeout
    summary: str
    content: str  # JSON text handed back to the LLM
    duration_ms: int
    phase: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    data: Any = None  # raw structured output (None on errors)
    tool_version: int | None = None


Handler = Callable[[Any, ToolContext], Awaitable[ToolOutput]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    handler: Handler
    timeout_s: float = 15.0
    phase: str = "thinking"  # conversation state while it runs ("retrieving" for knowledge search)
    intents: tuple[str, ...] = ()  # default intents when the DB registry is unavailable


_DROP = {"title", "minLength", "maxLength", "minimum", "maximum", "maxItems", "minItems", "additionalProperties"}


def _compact(node: Any) -> Any:
    """Minimal JSON schema for small LLMs (every token is re-read on each call): no titles, no length/range
    limits (pydantic still validates them on execution), `X | None` collapsed to X, no null defaults."""
    if isinstance(node, list):
        return [_compact(v) for v in node]
    if not isinstance(node, dict):
        return node
    if "anyOf" in node:
        non_null = [s for s in node["anyOf"] if s.get("type") != "null"]
        if len(non_null) == 1:
            merged = {**non_null[0], **{k: v for k, v in node.items() if k != "anyOf"}}
            return _compact(merged)
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k == "properties" and isinstance(v, dict):  # keys are field names (one may be called "title")
            out[k] = {name: _compact(s) for name, s in v.items()}
        elif k in _DROP or (k == "default" and v in (None, [], {})):
            continue
        else:
            out[k] = _compact(v)
    return out


def _short(v: Any, n: int) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"…(+{len(v) - n} chars)"
    if isinstance(v, dict):
        return {k: _short(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_short(x, n) for x in v[:20]]
    return v


def summarize_args(args: Any) -> str:
    return json.dumps(_short(args, 120), ensure_ascii=False, default=str)[:500]


DECLARATIVE_TIMEOUT_S = 10.0
MAX_TOOLS_PER_INTENT = 3


class ToolRegistry:
    """Code tools (`builtin`) + DB-configured tools. Reads are served from an in-memory snapshot loaded by
    `refresh()`; with no snapshot (no DB / empty table) the code definitions are used as-is."""

    def __init__(self, tools: list[ToolSpec], audit: AuditPort, *, knowledge: Any = None) -> None:
        self._code = {t.name: t for t in tools}
        self._audit = audit
        self._knowledge = knowledge
        self._repo: Any = None
        self._runner: Any = None
        self._rows: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------ wiring
    def attach(self, repo: Any, decrypt: Callable[[bytes], str], runner: Any = None) -> None:
        from .declarative import DeclarativeRunner  # local import: declarative imports this module

        self._repo = repo
        self._runner = runner or DeclarativeRunner(repo=repo, decrypt=decrypt, knowledge=self._knowledge)

    async def refresh(self) -> None:
        """Reload from the DB. Keeps the previous snapshot if the DB is unreachable."""
        if self._repo is None:
            return
        try:
            rows = await self._repo.list_tools()
        except Exception as e:
            log.warning("tool registry refresh failed: %s", type(e).__name__)
            return
        self._rows = {r["name"]: r for r in rows} if rows else None

    def code_tool_names(self) -> set[str]:
        return set(self._code)

    # ------------------------------------------------------------ read side
    def _entries(self) -> list[tuple[str, dict[str, Any] | None]]:
        """Enabled tools in a stable order: code tools (code order) first, then declarative by name."""
        if self._rows is None:
            return [(n, None) for n in self._code]
        out: list[tuple[str, dict[str, Any] | None]] = []
        for n in self._code:
            row = self._rows.get(n)
            if row is None:
                out.append((n, None))  # new code tool not seeded yet: keep it available
            elif row["enabled"]:
                out.append((n, row))
        for n in sorted(self._rows):
            row = self._rows[n]
            if n not in self._code and row["enabled"] and row["kind"] != "builtin":
                out.append((n, row))
        return out

    def _definition(self, name: str, row: dict[str, Any] | None) -> dict[str, Any]:
        spec = self._code.get(name)
        if row is None or row["kind"] == "builtin":
            assert spec is not None
            desc = row["description"] if row else spec.description
            params = _compact(spec.args.model_json_schema())
        else:
            desc, params = row["description"], _compact(row["params_schema"])
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}

    def definitions(self, exclude: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        """OpenAI tool definitions in a stable order (stable prompt prefix -> llama.cpp prompt cache hits)."""
        return [self._definition(n, r) for n, r in self._entries() if n not in exclude]

    def definitions_for(self, intent: str) -> list[dict[str, Any]]:
        """0-3 enabled tools whose `intents` contain `intent` (or '*')."""
        out = []
        for n, r in self._entries():
            intents = r["intents"] if r is not None else list(self._code[n].intents)
            if intent in intents or "*" in intents:
                out.append(self._definition(n, r))
            if len(out) >= MAX_TOOLS_PER_INTENT:
                break
        return out

    def names(self) -> list[str]:
        return [n for n, _ in self._entries()]

    # ------------------------------------------------------------ execution
    async def _log(
        self,
        ctx: ToolContext,
        name: str,
        args: Any,
        summary: str,
        status: str,
        version: int | None,
        ms: int,
        test: bool,
    ) -> None:
        try:
            extra = {"tool_version": version, "duration_ms": ms, "authorized_by": "ui_test" if test else None}
            try:
                await self._audit.record(
                    ctx.session_id, name, summarize_args(args), summary[:_SUMMARY_CHARS], status, **extra
                )
            except TypeError:  # audit implementation without the extra columns
                await self._audit.record(ctx.session_id, name, summarize_args(args), summary[:_SUMMARY_CHARS], status)
        except Exception as e:  # auditing must never break a turn
            log.warning("tool audit failed for %s: %s", name, type(e).__name__)

    async def execute(self, name: str, arguments: Any, ctx: ToolContext) -> ToolResult:
        return await self._execute(name, arguments, ctx, test=False, row=None)

    async def run_test(self, row: dict[str, Any], arguments: Any, ctx: ToolContext) -> ToolResult:
        """Test console: runs a (possibly disabled) tool straight from its DB row."""
        return await self._execute(row["name"], arguments, ctx, test=True, row=row)

    async def _execute(self, name: str, arguments: Any, ctx: ToolContext, *, test: bool, row: dict[str, Any] | None):
        t0 = time.perf_counter()
        version = None

        def done(ok: bool, status: str, summary: str, data: Any = None, phase: str = "thinking", sources=None):
            payload = data if data is not None else {"error": summary}
            content = json.dumps(payload, ensure_ascii=False, default=str)
            if len(content) > MAX_CONTENT_CHARS:
                content = content[:MAX_CONTENT_CHARS] + " …[truncado]"
            return ToolResult(
                name, ok, status, summary, content, round((time.perf_counter() - t0) * 1000), phase, sources or [],
                data if ok else None, version,
            )  # fmt: skip

        async def finish(r: ToolResult) -> ToolResult:
            await self._log(ctx, name, arguments, r.summary, r.status, version, r.duration_ms, test)
            return r

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        spec = self._code.get(name)
        if row is None and self._rows is not None:
            row = self._rows.get(name)
        if row is None and spec is None:
            return await finish(done(False, "denied", f"herramienta desconocida: {name}"))
        if row is not None:
            version = row.get("version")
            if not row["enabled"] and not test:
                return await finish(done(False, "denied", f"herramienta desactivada: {name}"))
        kind = row["kind"] if row is not None else "builtin"

        if kind == "builtin":
            if spec is None:
                return await finish(done(False, "error", "implementación no disponible"))
            try:
                args = spec.args.model_validate(arguments)
            except ValidationError as e:
                msg = "; ".join(f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in e.errors())
                return await finish(done(False, "error", f"argumentos inválidos: {msg}", phase=spec.phase))
            call, timeout, phase = spec.handler(args, ctx), spec.timeout_s, spec.phase
        else:
            if self._runner is None or row is None:
                return await finish(done(False, "error", "herramientas configurables no disponibles"))
            errs = sorted(Draft202012Validator(row["params_schema"]).iter_errors(arguments), key=lambda e: list(e.path))
            if errs:
                msg = "; ".join(f"{'.'.join(str(p) for p in e.path) or 'args'}: {e.message}" for e in errs[:3])
                return await finish(done(False, "error", f"argumentos inválidos: {msg}"[:300]))
            phase = "retrieving" if kind == "rag_search" else "thinking"
            timeout = 30.0 if kind == "rag_search" else DECLARATIVE_TIMEOUT_S
            call = self._runner.run(row, arguments, ctx)
        try:
            out = await asyncio.wait_for(call, timeout)
        except TimeoutError:
            r = done(False, "timeout", f"tiempo agotado ({timeout:g}s)", phase=phase)
        except asyncio.CancelledError:
            await asyncio.shield(self._log(ctx, name, arguments, "cancelado", "error", version, 0, test))
            raise
        except Exception as e:
            log.warning("tool %s failed: %s", name, type(e).__name__)
            r = done(False, "error", f"{type(e).__name__}: {e}"[:200], phase=phase)
        else:
            r = done(True, "ok", out.summary, out.data, phase, out.sources)
        return await finish(r)
