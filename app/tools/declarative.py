"""Executors for the declarative tool kinds. No arbitrary code, no free SQL, no free HTTP."""

import json
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from jinja2 import StrictUndefined
from jinja2.exceptions import SecurityError, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from app.conversation.ports import KnowledgeSearchPort
from app.knowledge.chunking import excerpt

from .netguard import BlockedRequestError, safe_fetch
from .registry import ToolContext, ToolOutput
from .sqlguard import MAX_ROWS, validate_select

MAX_TEMPLATE_CHARS = 4000
MAX_OUTPUT_CHARS = 4000
CLIENT_ACTIONS = frozenset({"browser_connectivity_check", "browser_speed_test"})
RAG_FILTERS = ("document_type", "language", "is_synthetic")
_PATH_PART = re.compile(r"^[A-Za-z0-9_\-]+$")


class _Env(ImmutableSandboxedEnvironment):
    """Sandbox without globals (range, lipsum, ...) and with bounded `*` / `**` (memory bombs)."""

    intercepted_binops = frozenset({"*", "**"})

    def call_binop(self, context, operator, left, right):  # type: ignore[no-untyped-def]
        if operator == "*" and isinstance(left, int | float) and isinstance(right, int | float):
            pass
        elif operator == "*":
            n = right if isinstance(right, int) else left
            if isinstance(n, int) and n > 1000:
                raise SecurityError("multiplicación de texto/lista demasiado grande")
        elif operator == "**" and (not isinstance(right, int) or right > 32):
            raise SecurityError("exponente demasiado grande")
        return super().call_binop(context, operator, left, right)


def make_env() -> _Env:
    env = _Env(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
    env.globals = {}
    env.filters["urlq"] = lambda v: quote(str(v), safe="")
    return env


_ENV = make_env()


def check_template(src: str) -> None:
    if len(src) > MAX_TEMPLATE_CHARS:
        raise ValueError(f"la plantilla supera {MAX_TEMPLATE_CHARS} caracteres")
    try:
        _ENV.parse(src)
    except TemplateError as e:
        raise ValueError(f"plantilla inválida: {e}") from e


def render(src: str, values: dict[str, Any]) -> str:
    try:
        return _ENV.from_string(src).render(**values)
    except (TemplateError, SecurityError) as e:
        raise ValueError(f"no se pudo generar la respuesta: {type(e).__name__}: {e}"[:200]) from e


def extract_path(data: Any, path: str) -> Any:
    """Dotted path: `$.status`, `data.items.0.name`. No expressions, no filters."""
    p = path.strip()
    if p.startswith("$"):
        p = p[1:].lstrip(".")
    cur = data
    for part in [x for x in p.split(".") if x != ""]:
        if not _PATH_PART.match(part):
            raise ValueError(f"ruta inválida: {part!r}")
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError) as e:
                raise ValueError(f"no existe el índice {part!r} en la respuesta") from e
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise ValueError(f"no existe {part!r} en la respuesta")
    return cur


class ToolsRepoPort(Protocol):
    async def run_select(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]: ...

    async def secret_ciphertext(self, ref: str) -> bytes | None: ...


class DeclarativeRunner:
    def __init__(
        self,
        *,
        repo: ToolsRepoPort,
        decrypt: Callable[[bytes], str],
        knowledge: KnowledgeSearchPort | None = None,
        fetch: Callable[..., Any] = safe_fetch,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Any = None,
    ) -> None:
        self._repo, self._decrypt, self._knowledge = repo, decrypt, knowledge
        self._fetch, self._transport, self._resolver = fetch, transport, resolver

    async def run(self, row: dict[str, Any], args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        kind, cfg = row["kind"], row.get("config") or {}
        if kind == "static_template":
            return self._static(cfg, args)
        if kind == "rag_search":
            return await self._rag(cfg, args)
        if kind == "sql_readonly":
            return await self._sql(cfg, args)
        if kind == "http_request":
            return await self._http(cfg, args)
        if kind == "client_tool":
            action = cfg.get("client_action")
            return ToolOutput(
                {"needs_client": True, "client_action": action, "args": args},
                f"requiere ejecución en el navegador ({action})",
            )
        raise ValueError(f"tipo de herramienta no ejecutable: {kind}")

    # -- static_template
    def _static(self, cfg: dict[str, Any], args: dict[str, Any]) -> ToolOutput:
        text = render(str(cfg.get("template", "")), args)[:MAX_OUTPUT_CHARS]
        return ToolOutput({"text": text}, text[:120])

    # -- rag_search
    async def _rag(self, cfg: dict[str, Any], args: dict[str, Any]) -> ToolOutput:
        if self._knowledge is None:
            raise ValueError("búsqueda de conocimiento no disponible")
        top_k = min(max(int(cfg.get("top_k", 3)), 1), 10)
        filters = {k: v for k, v in (cfg.get("filters") or {}).items() if k in RAG_FILTERS and v not in (None, "")}
        hits = await self._knowledge.search(str(args["query"])[:500], top_k, filters or None)
        sources = [
            {
                "title": h.title, "source_uri": h.source_uri, "section": h.section, "score": round(h.score, 4),
                "is_synthetic": h.is_synthetic, "document_id": h.document_id, "chunk_id": h.chunk_id,
                "chunk_index": getattr(h, "chunk_index", None), "excerpt": excerpt(h.content),
            }
            for h in hits
        ]  # fmt: skip
        data: dict[str, Any] = {
            "hits": [{**s, "content": h.content[:1200]} for s, h in zip(sources, hits, strict=True)]
        }
        if not hits:
            data["note"] = "Sin resultados en la base de conocimiento."
        return ToolOutput(data, f"{len(hits)} fragmento(s) encontrados", sources)

    # -- sql_readonly
    async def _sql(self, cfg: dict[str, Any], args: dict[str, Any]) -> ToolOutput:
        query = str(cfg.get("query", ""))
        names = validate_select(query)  # validated again at run time
        params: dict[str, Any] = {}
        for n in names:
            v = args.get(n)
            if isinstance(v, dict | set) or (isinstance(v, list) and any(isinstance(x, dict | list) for x in v)):
                raise ValueError(f"parámetro {n} con tipo no permitido")
            params[n] = v
        rows = (await self._repo.run_select(query, params))[:MAX_ROWS]
        return ToolOutput({"rows": rows, "count": len(rows)}, f"{len(rows)} fila(s)")

    # -- http_request
    async def _http(self, cfg: dict[str, Any], args: dict[str, Any]) -> ToolOutput:
        quoted = {k: quote(str(v), safe="") for k, v in args.items()}
        url = render(str(cfg["url"]), quoted)
        body = None
        if cfg.get("body_template"):
            body = render(str(cfg["body_template"]), args)  # use {{ x|tojson }} in the template
        headers: dict[str, str] = {}
        secret: str | None = None
        if cfg.get("secret_ref"):
            token = await self._repo.secret_ciphertext(str(cfg["secret_ref"]))
            if token is None:
                raise ValueError("el secreto configurado no existe")
            secret = self._decrypt(token)
            scheme = cfg.get("secret_scheme", "Bearer")
            headers[cfg.get("secret_header") or "Authorization"] = f"{scheme} {secret}".strip()
        if body is not None:
            headers["Content-Type"] = "application/json"
        extra: dict[str, Any] = {"transport": self._transport}
        if self._resolver is not None:
            extra["resolver"] = self._resolver
        try:
            status, _h, raw = await self._fetch(
                cfg.get("method", "GET"), url, allowed_hosts=list(cfg.get("allowed_hosts") or []),
                allow_http=bool(cfg.get("allow_http", False)), headers=headers, body=body, **extra,
            )  # fmt: skip
        except BlockedRequestError:
            raise
        except Exception as e:  # never echo transport errors: they may carry URLs/headers
            raise ValueError(f"falló la solicitud ({type(e).__name__})") from None
        text = raw.decode("utf-8", errors="replace")
        if secret:
            text = text.replace(secret, "[redacted]")
        if not 200 <= status < 300:
            raise ValueError(f"el servicio respondió HTTP {status}")
        result: Any = text
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if cfg.get("response_path"):
            if parsed is None:
                raise ValueError("la respuesta no es JSON; no se puede aplicar la ruta")
            result = extract_path(parsed, cfg["response_path"])
        elif parsed is not None:
            result = parsed
        if isinstance(result, str):
            result = result[:MAX_OUTPUT_CHARS]
        return ToolOutput({"status_code": status, "result": result}, f"HTTP {status}")
