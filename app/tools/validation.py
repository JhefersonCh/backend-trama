"""Save-time validation of tool definitions (kind-specific `config`, `params_schema`, `intents`)."""

import re
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .declarative import CLIENT_ACTIONS, RAG_FILTERS, check_template
from .netguard import ALLOWED_METHODS, check_url
from .sqlguard import SqlRejectedError, validate_select

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
INTENT_RE = re.compile(r"^(\*|[a-z][a-z_]{0,29})$")
HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]{0,39}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-\.]{0,251}[a-z0-9])?$")
SECRET_REF_RE = re.compile(r"^provider:[\w\-\. ]{1,100}$")
MAX_SCHEMA_CHARS = 8000
MAX_PROPS = 10

CONFIG_KEYS: dict[str, set[str]] = {
    "builtin": set(),
    "static_template": {"template"},
    "rag_search": {"filters", "top_k"},
    "sql_readonly": {"query"},
    "http_request": {
        "method",
        "url",
        "allowed_hosts",
        "allow_http",
        "secret_ref",
        "secret_header",
        "secret_scheme",
        "response_path",
        "body_template",
    },  # fmt: skip
    "client_tool": {"client_action"},
}


class ToolValidationError(ValueError):
    pass


def _walk_refs(node: Any) -> bool:
    if isinstance(node, dict):
        return any(k in ("$ref", "$dynamicRef", "$id", "$anchor") or _walk_refs(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_walk_refs(v) for v in node)
    return False


def validate_params_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ToolValidationError('params_schema debe ser un JSON Schema con "type": "object"')
    if len(str(schema)) > MAX_SCHEMA_CHARS:
        raise ToolValidationError("params_schema es demasiado grande")
    if _walk_refs(schema):
        raise ToolValidationError("params_schema no admite referencias ($ref)")
    props = schema.get("properties", {})
    if not isinstance(props, dict) or len(props) > MAX_PROPS:
        raise ToolValidationError(f"params_schema admite hasta {MAX_PROPS} parámetros")
    for k in props:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$", k):
            raise ToolValidationError(f"nombre de parámetro inválido: {k!r}")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise ToolValidationError(f"params_schema inválido: {e.message}") from None
    return schema


def validate_intents(intents: Any) -> list[str]:
    if not isinstance(intents, list) or len(intents) > 12 or not all(isinstance(i, str) for i in intents):
        raise ToolValidationError("intents debe ser una lista de textos")
    clean = list(dict.fromkeys(i.strip().lower() for i in intents if i.strip()))
    for i in clean:
        if not INTENT_RE.match(i):
            raise ToolValidationError(f"intención inválida: {i!r}")
    return clean


def validate_config(kind: str, config: Any, params_schema: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    if not isinstance(config, dict):
        raise ToolValidationError("config debe ser un objeto")
    extra = set(config) - CONFIG_KEYS[kind]
    if extra:
        raise ToolValidationError(f"config contiene claves no permitidas: {', '.join(sorted(extra))}")
    props = set(params_schema.get("properties", {}))
    if kind == "static_template":
        t = config.get("template")
        if not isinstance(t, str) or not t.strip():
            raise ToolValidationError("falta la plantilla")
        try:
            check_template(t)
        except ValueError as e:
            raise ToolValidationError(str(e)) from None
    elif kind == "rag_search":
        if "query" not in props:
            raise ToolValidationError('params_schema debe declarar el parámetro "query"')
        f = config.get("filters", {})
        if not isinstance(f, dict) or set(f) - set(RAG_FILTERS):
            raise ToolValidationError(f"filtros permitidos: {', '.join(RAG_FILTERS)}")
        k = config.get("top_k", 3)
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 10:
            raise ToolValidationError("top_k debe estar entre 1 y 10")
    elif kind == "sql_readonly":
        try:
            used = validate_select(str(config.get("query", "")))
        except SqlRejectedError as e:
            raise ToolValidationError(str(e)) from None
        if used - props:
            raise ToolValidationError(f"la consulta usa parámetros no declarados: {', '.join(sorted(used - props))}")
    elif kind == "http_request":
        _validate_http(config)
    elif kind == "client_tool":
        if config.get("client_action") not in CLIENT_ACTIONS:
            raise ToolValidationError(f"client_action permitido: {', '.join(sorted(CLIENT_ACTIONS))}")
    return config


def _validate_http(c: dict[str, Any]) -> None:
    method = str(c.get("method", "GET")).upper()
    if method not in ALLOWED_METHODS:
        raise ToolValidationError("método permitido: GET o POST")
    hosts = c.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts or len(hosts) > 10:
        raise ToolValidationError("allowed_hosts debe tener entre 1 y 10 hosts")
    for h in hosts:
        if not isinstance(h, str) or not HOST_RE.match(h.lower()):
            raise ToolValidationError(f"host inválido: {h!r} (sin esquema, sin comodines)")
    url = c.get("url")
    if not isinstance(url, str) or not url:
        raise ToolValidationError("falta la URL")
    host_part = urlsplit(url)  # the host must be literal in the template (not built from parameters)
    if "{" in (host_part.netloc or "") or not host_part.hostname:
        raise ToolValidationError("el host de la URL debe ser fijo (sin variables)")
    try:
        check_url(re.sub(r"\{\{.*?\}\}", "x", url), hosts, bool(c.get("allow_http", False)))
        check_template(url)
        if c.get("body_template"):
            check_template(str(c["body_template"]))
    except Exception as e:  # incl. BlockedRequestError
        raise ToolValidationError(str(e)) from None
    if c.get("allow_http") is not None and not isinstance(c["allow_http"], bool):
        raise ToolValidationError("allow_http debe ser verdadero o falso")
    ref = c.get("secret_ref")
    if ref is not None and not (isinstance(ref, str) and SECRET_REF_RE.match(ref)):
        raise ToolValidationError('secret_ref debe tener la forma "provider:<nombre del proveedor>"')
    hdr = c.get("secret_header")
    if hdr is not None and (not isinstance(hdr, str) or not HEADER_RE.match(hdr)):
        raise ToolValidationError("secret_header inválido")
    sch = c.get("secret_scheme")
    if sch is not None and (not isinstance(sch, str) or len(sch) > 20 or not re.match(r"^[A-Za-z0-9\-]*$", sch)):
        raise ToolValidationError("secret_scheme inválido")
    path = c.get("response_path")
    if path is not None and (not isinstance(path, str) or len(path) > 200 or not re.match(r"^[\$\.\w\-]*$", path)):
        raise ToolValidationError("response_path inválido (use una ruta con puntos, por ejemplo $.status)")
