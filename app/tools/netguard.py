"""Anti-SSRF outbound HTTP for `http_request` tools.

Exact-match host allowlist, https only (http only with explicit opt-in), DNS resolved by us and EVERY address must
be public, the connection is pinned to the validated IP (no rebinding), no redirects, 5 s timeout, 256 KB cap."""

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

MAX_BODY_BYTES = 256 * 1024
TIMEOUT_S = 5.0
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
ALLOWED_METHODS = frozenset({"GET", "POST"})


class BlockedRequestError(Exception):
    """Request refused by the SSRF policy (message is safe to show)."""


def ip_is_blocked(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    a = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    if isinstance(a, ipaddress.IPv6Address) and a.sixtofour is not None and ip_is_blocked(a.sixtofour):
        return True
    return (
        a.is_private
        or a.is_loopback
        or a.is_link_local
        or a.is_multicast
        or a.is_reserved
        or a.is_unspecified
        or not a.is_global
    )


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


def check_url(url: str, allowed_hosts: list[str], allow_http: bool = False) -> tuple[str, str, int | None, str]:
    """Static checks. Returns (scheme, host, port, path_and_query)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("https", "http"):
        raise BlockedRequestError("solo se permiten URLs https")
    if parts.username or parts.password:
        raise BlockedRequestError("la URL no puede incluir credenciales")
    if not host or host not in {h.lower() for h in allowed_hosts}:
        raise BlockedRequestError(f"host no permitido: {host or '(vacío)'}")
    if scheme == "http" and not allow_http:
        raise BlockedRequestError("http no está habilitado para esta herramienta; use https")
    try:
        port = parts.port
    except ValueError as e:
        raise BlockedRequestError("puerto inválido") from e
    if port is not None and port not in ALLOWED_PORTS:
        raise BlockedRequestError(f"puerto no permitido: {port}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path


async def safe_fetch(
    method: str,
    url: str,
    *,
    allowed_hosts: list[str],
    allow_http: bool = False,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = TIMEOUT_S,
    max_bytes: int = MAX_BODY_BYTES,
) -> tuple[int, dict[str, str], bytes]:
    method = method.upper()
    if method not in ALLOWED_METHODS:
        raise BlockedRequestError(f"método no permitido: {method}")
    scheme, host, port, path = check_url(url, allowed_hosts, allow_http)
    real_port = port or (443 if scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
        addrs = [str(literal)]
    except ValueError:
        try:
            addrs = await resolver(host, real_port)
        except OSError as e:
            raise BlockedRequestError("no se pudo resolver el host") from e
    if not addrs:
        raise BlockedRequestError("el host no resuelve a ninguna dirección")
    for a in addrs:
        if ip_is_blocked(a):
            raise BlockedRequestError("el host resuelve a una dirección no pública (bloqueado)")
    ip = addrs[0]
    ip_part = f"[{ip}]" if ":" in ip else ip
    target = f"{scheme}://{ip_part}:{real_port}{path}"
    hdrs = {k: v for k, v in (headers or {}).items()}
    hdrs["Host"] = host if port is None else f"{host}:{port}"
    hdrs.setdefault("Accept", "application/json, text/plain;q=0.8")
    hdrs.setdefault("User-Agent", "trama-tools/1")
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, timeout=timeout, trust_env=False, verify=True
    ) as client:
        req = client.build_request(
            method, target, headers=hdrs, content=body.encode() if body else None,
            extensions={"sni_hostname": host},
        )  # fmt: skip
        resp = await asyncio.wait_for(client.send(req, stream=True), timeout)
        try:
            if 300 <= resp.status_code < 400:
                raise BlockedRequestError("redirección bloqueada")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise BlockedRequestError(f"la respuesta supera {max_bytes // 1024} KB")
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise BlockedRequestError("tiempo agotado leyendo la respuesta")
            return resp.status_code, dict(resp.headers), b"".join(chunks)
        finally:
            await resp.aclose()
