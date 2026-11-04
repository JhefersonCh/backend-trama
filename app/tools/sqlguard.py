"""Static guard for `sql_readonly` tools. Defense in depth: the real barrier is the PostgreSQL role
`trama_tools_ro` (SELECT only on whitelisted views, read-only transaction, 2 s timeout).

Accepts exactly ONE `SELECT` over whitelisted views, with named params `%(name)s`, no comments, no quoted
identifiers, no subquery in FROM, no comma joins, only allow-listed functions and a literal `LIMIT <= 50`."""

import re

MAX_ROWS = 50
MAX_QUERY_CHARS = 2000
ALLOWED_VIEWS = frozenset({"v_tools_technicians_available", "v_tools_open_incidents_by_zone"})


class SqlRejectedError(ValueError):
    pass


_TOKEN = re.compile(
    r"""(?P<ws>\s+)
      | (?P<str>'(?:[^']|'')*')
      | %\((?P<param>[A-Za-z_][A-Za-z0-9_]*)\)s
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<num>\d+(?:\.\d+)?)
      | (?P<op>::|<=|>=|<>|!=|\|\||[(),.*=<>+\-/])
    """,
    re.X,
)

_FORBIDDEN = frozenset(
    """insert update delete drop alter create grant revoke copy call do truncate execute into set reset vacuum
    analyze listen notify lock refresh merge with comment security explain prepare deallocate declare fetch move
    discard load import checkpoint cluster reindex abort begin commit rollback savepoint release start returning
    for table regclass regproc regprocedure regoper regnamespace regtype oid current_user session_user user
    current_role current_catalog current_database current_schema""".split()
)
_FUNCTIONS = frozenset(
    """count min max sum avg lower upper coalesce nullif now date_trunc length trim round concat left right substr
    cardinality array_length any all lpad""".split()
)
# words that may precede "(" without being function calls
_KW_BEFORE_PAREN = frozenset(
    "in and or not on as from where join select by when then else over filter between case having using exists".split()
)
_CLAUSE_END = frozenset("where group order having limit offset union intersect except".split())


def _tokens(query: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    while pos < len(query):
        m = _TOKEN.match(query, pos)
        if not m:
            raise SqlRejectedError(f"carácter no permitido en la posición {pos}: {query[pos]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        if kind == "param":
            out.append(("param", m.group("param")))
        elif kind == "word":
            out.append(("word", m.group("word").lower()))
        else:
            out.append((kind or "op", m.group(0)))
    return out


def validate_select(query: str, allowed_views: frozenset[str] = ALLOWED_VIEWS) -> set[str]:
    """Raises SqlRejectedError. Returns the named parameters used by the query."""
    if not isinstance(query, str) or not query.strip():
        raise SqlRejectedError("la consulta está vacía")
    if len(query) > MAX_QUERY_CHARS:
        raise SqlRejectedError(f"la consulta supera {MAX_QUERY_CHARS} caracteres")
    no_lit = re.sub(r"'(?:[^']|'')*'", "''", query)
    if "--" in no_lit or "/*" in no_lit or "*/" in no_lit:
        raise SqlRejectedError("no se permiten comentarios")
    if "%" in re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "", query):
        raise SqlRejectedError("solo se permiten parámetros con nombre: %(nombre)s")
    toks = _tokens(query)
    if not toks or toks[0] != ("word", "select"):
        raise SqlRejectedError("solo se permite una consulta SELECT")

    params: set[str] = set()
    depth = 0
    from_depth: int | None = None
    for i, (kind, val) in enumerate(toks):
        if kind == "param":
            params.add(val)
            continue
        if kind == "op":
            if val == "(":
                depth += 1
            elif val == ")":
                depth -= 1
                if from_depth is not None and depth < from_depth:
                    from_depth = None
            elif val == "," and from_depth is not None and depth == from_depth:
                raise SqlRejectedError("no se permiten varias tablas separadas por coma; use JOIN")
            continue
        if kind != "word":
            continue
        if val in _FORBIDDEN:
            raise SqlRejectedError(f"palabra no permitida: {val}")
        if val.startswith(("pg_", "lo_", "information_schema", "dblink")) or "pg_" in val:
            raise SqlRejectedError(f"objeto no permitido: {val}")
        nxt = toks[i + 1] if i + 1 < len(toks) else ("end", "")
        if nxt == ("op", "(") and val not in _FUNCTIONS and val not in _KW_BEFORE_PAREN:
            raise SqlRejectedError(f"función no permitida: {val}")
        if val in _CLAUSE_END and from_depth == depth:
            from_depth = None
        if val in ("from", "join"):
            if nxt[0] != "word" or nxt[1] not in allowed_views:
                raise SqlRejectedError("solo se pueden consultar: " + ", ".join(sorted(allowed_views)))
            if i + 2 < len(toks) and toks[i + 2] == ("op", "."):
                raise SqlRejectedError("no se permiten nombres calificados con esquema")
            if val == "from":
                from_depth = depth
        if val == "limit":
            if nxt[0] != "num" or not nxt[1].isdigit() or not 1 <= int(nxt[1]) <= MAX_ROWS:
                raise SqlRejectedError(f"LIMIT debe ser un número entre 1 y {MAX_ROWS}")
    if depth != 0:
        raise SqlRejectedError("paréntesis desbalanceados")
    if len(toks) < 2 or toks[-2] != ("word", "limit"):
        raise SqlRejectedError(f"la consulta debe terminar en LIMIT <= {MAX_ROWS}")
    return params
