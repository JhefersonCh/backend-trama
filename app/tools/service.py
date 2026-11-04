import time
from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from app.config.errors import DomainValidationError

from .models import ToolCreate, ToolPatch, ToolTestResult
from .registry import ToolContext, ToolRegistry
from .validation import ToolValidationError, validate_config, validate_intents, validate_params_schema

TEST_LIMIT = 10  # per tool per minute
TEST_WINDOW_S = 60.0
BUILTIN_EDITABLE = {"description", "intents", "enabled"}


class RateLimitedError(Exception):
    pass


class RateLimiter:
    def __init__(self, limit: int = TEST_LIMIT, window: float = TEST_WINDOW_S, clock=time.monotonic) -> None:
        self._limit, self._window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> None:
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] > self._window:
            q.popleft()
        if len(q) >= self._limit:
            raise RateLimitedError(f"límite de {self._limit} pruebas por minuto para esta herramienta")
        q.append(now)


class ToolsService:
    def __init__(self, repo: Any, registry: ToolRegistry, limiter: RateLimiter) -> None:
        self._repo, self._registry, self._limiter = repo, registry, limiter

    async def list_all(self) -> list[dict[str, Any]]:
        return await self._repo.list_tools()

    async def get(self, id: UUID) -> dict[str, Any]:
        return await self._repo.get(id)

    async def create(self, body: ToolCreate) -> dict[str, Any]:
        if body.kind == "builtin":
            raise DomainValidationError("las herramientas integradas se registran por código; elija otro tipo")
        if body.name in self._registry.code_tool_names():
            raise DomainValidationError(f"el nombre {body.name!r} está reservado")
        try:
            schema = validate_params_schema(body.params_schema)
            data = {
                "name": body.name, "kind": body.kind, "description": body.description, "params_schema": schema,
                "config": validate_config(body.kind, body.config, schema),
                "intents": validate_intents(body.intents), "enabled": body.enabled,
            }  # fmt: skip
        except ToolValidationError as e:
            raise DomainValidationError(str(e)) from None
        row = await self._repo.insert(data)
        await self._registry.refresh()
        return row

    async def patch(self, id: UUID, body: ToolPatch) -> dict[str, Any]:
        cur = await self._repo.get(id)
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            return cur
        try:
            if cur["kind"] == "builtin":
                bad = set(changes) - BUILTIN_EDITABLE
                if bad:
                    raise ToolValidationError("integrada: solo se edita descripción, intenciones y estado")
            if "params_schema" in changes:
                changes["params_schema"] = validate_params_schema(changes["params_schema"])
            if "intents" in changes:
                changes["intents"] = validate_intents(changes["intents"])
            if cur["kind"] != "builtin" and ({"config", "params_schema"} & set(changes)):
                schema = changes.get("params_schema", cur["params_schema"])
                changes["config"] = validate_config(cur["kind"], changes.get("config", cur["config"]), schema)
        except ToolValidationError as e:
            raise DomainValidationError(str(e)) from None
        row = await self._repo.update(id, changes)
        await self._registry.refresh()
        return row

    async def toggle(self, id: UUID) -> dict[str, Any]:
        cur = await self._repo.get(id)
        row = await self._repo.set_enabled(id, not cur["enabled"])
        await self._registry.refresh()
        return row

    async def versions(self, id: UUID) -> list[dict[str, Any]]:
        return await self._repo.versions(id)

    async def test(self, id: UUID, args: dict[str, Any]) -> ToolTestResult:
        row = await self._repo.get(id)
        self._limiter.hit(str(id))
        res = await self._registry.run_test(row, args, ToolContext(session_id=None))
        if res.ok:
            return ToolTestResult(ok=True, output=res.data, duration_ms=res.duration_ms)
        return ToolTestResult(ok=False, output=None, duration_ms=res.duration_ms, error=res.summary)
