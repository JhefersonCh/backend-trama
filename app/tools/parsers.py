# ruff: noqa: E501
"""Deterministic parsers for pasted command output (no LLM involved).

Supported: Windows `ipconfig /all`, Linux `ip addr` / `ip route`, `ping` (Windows + Linux), `nslookup`.
English and Spanish (Windows ES) outputs are understood. Every parser returns a ParsedOutput.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


class UnsupportedCommandError(ValueError):
    pass


@dataclass
class ParsedOutput:
    kind: str
    parsed: dict[str, Any]
    findings: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""
    observations: list[dict[str, Any]] = field(default_factory=list)  # {observation_type, target, result, unit?}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _is_apipa(ip: str | None) -> bool:
    return bool(ip) and ip.startswith("169.254.")  # type: ignore[union-attr]


def _finding(code: str, detail: str, **extra: str) -> dict[str, str]:
    return {"code": code, "detail": detail, **extra}


# ---------------------------------------------------------------- ipconfig /all
_HEADER = re.compile(r"^\S.*:\s*$")
_KV = re.compile(r"^\s+(.+?)[ .]*:\s?(.*)$")
_CONT = re.compile(r"^\s+([0-9a-fA-F:.%]+)(\(.*\))?\s*$")


def _classify(key: str) -> str | None:
    k = _norm(key)
    if "ipv4" in k:
        return "apipa_ipv4" if "autoconfig" in k else "ipv4"
    if "subnet mask" in k or "mascara de subred" in k:
        return "mask"
    if "default gateway" in k or "puerta de enlace" in k:
        return "gateway"
    if k.startswith("dhcp enabled") or k.startswith("dhcp habilitado"):
        return "dhcp_enabled"
    if "dhcp server" in k or "servidor dhcp" in k:
        return "dhcp_server"
    if "dns servers" in k or "servidores dns" in k:
        return "dns"
    if "media state" in k or "estado de los medios" in k:
        return "media_state"
    return None


def _clean_value(v: str) -> str:
    return re.sub(r"\(.*?\)", "", v).strip()


def parse_ipconfig(raw: str) -> ParsedOutput:
    blocks: list[tuple[str, dict[str, list[str]]]] = []
    last: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            last = None
            if _HEADER.match(line):
                blocks.append((line.strip().rstrip(":").strip(), {}))
            continue
        if not blocks:
            continue
        data = blocks[-1][1]
        cont = _CONT.match(line)
        if cont and last:
            data.setdefault(last, []).append(_clean_value(cont.group(1)))
            continue
        m = _KV.match(line)
        if not m:
            if "desconectado" in _norm(line) or "disconnected" in _norm(line):  # "Medios desconectados"
                data.setdefault("media_state", []).append("disconnected")
            continue
        last = _classify(m.group(1))
        if last:
            val = _clean_value(m.group(2))
            data.setdefault(last, [])
            if val:
                data[last].append(val)

    adapters: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    for name, d in blocks:
        ipv4 = (d.get("ipv4") or d.get("apipa_ipv4") or [None])[0]
        dhcp_raw = (d.get("dhcp_enabled") or [None])[0]
        media = " ".join(d.get("media_state", []))
        adapter = {
            "adapter": name,
            "ipv4": ipv4,
            "mask": (d.get("mask") or [None])[0],
            "gateway": [g for g in d.get("gateway", []) if _IPV4.fullmatch(g)],
            "dhcp_enabled": None if dhcp_raw is None else _norm(dhcp_raw) in ("yes", "si", "true"),
            "dhcp_server": (d.get("dhcp_server") or [None])[0],
            "dns": [x for x in d.get("dns", []) if _IPV4.fullmatch(x)],
            "media_disconnected": "disconnected" in _norm(media) or "desconectado" in _norm(media),
            "apipa": _is_apipa(ipv4),
        }
        if not (ipv4 or adapter["media_disconnected"] or adapter["dhcp_enabled"] is not None):
            continue
        adapters.append(adapter)
        if adapter["apipa"]:
            findings.append(
                _finding(
                    "apipa",
                    f"{name}: IP {ipv4} (APIPA 169.254.0.0/16). El equipo no obtuvo dirección por DHCP.",
                    adapter=name,
                )
            )
        elif ipv4 and not adapter["gateway"]:
            findings.append(_finding("no_gateway", f"{name}: tiene IP {ipv4} pero no puerta de enlace.", adapter=name))
        if ipv4 and not adapter["apipa"] and not adapter["dns"]:
            findings.append(_finding("no_dns", f"{name}: sin servidores DNS configurados.", adapter=name))
        if adapter["dhcp_enabled"] is False and ipv4 and not adapter["apipa"]:
            findings.append(_finding("static_ip", f"{name}: IP estática (DHCP deshabilitado).", adapter=name))
        if adapter["media_disconnected"]:
            findings.append(
                _finding("media_disconnected", f"{name}: medios desconectados (cable/Wi-Fi).", adapter=name)
            )
    if not adapters:
        raise UnsupportedCommandError("no se reconoció ningún adaptador en la salida de ipconfig")
    summary = f"ipconfig: {len(adapters)} adaptador(es). " + (
        " ".join(f["detail"] for f in findings) if findings else "Sin hallazgos anómalos."
    )
    obs = [{"observation_type": "ipconfig_adapter", "target": a["adapter"], "result": a} for a in adapters]
    return ParsedOutput("ipconfig", {"adapters": adapters}, findings, summary, obs)


# ---------------------------------------------------------------- ip addr / ip route
_IP_IFACE = re.compile(r"^\d+:\s+([^:\s]+):\s+<([^>]*)>.*?(?:state\s+(\w+))?")
_IP_INET = re.compile(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)")
_IP_MAC = re.compile(r"^\s+link/ether\s+([0-9a-fA-F:]{17})")


def parse_ip_addr(raw: str) -> ParsedOutput:
    ifaces: list[dict[str, Any]] = []
    for line in raw.splitlines():
        m = _IP_IFACE.match(line)
        if m:
            state = re.search(r"state\s+(\w+)", line)
            ifaces.append(
                {
                    "name": m.group(1).split("@")[0],
                    "flags": m.group(2).split(","),
                    "state": state.group(1) if state else None,
                    "ipv4": [],
                    "mac": None,
                    "apipa": False,
                }
            )
            continue
        if not ifaces:
            continue
        m = _IP_INET.match(line)
        if m:
            ifaces[-1]["ipv4"].append(f"{m.group(1)}/{m.group(2)}")
            if _is_apipa(m.group(1)):
                ifaces[-1]["apipa"] = True
            continue
        m = _IP_MAC.match(line)
        if m:
            ifaces[-1]["mac"] = m.group(1)
    if not ifaces:
        raise UnsupportedCommandError("no se reconoció salida de `ip addr`")
    findings: list[dict[str, str]] = []
    for i in ifaces:
        if i["name"] == "lo":
            continue
        if i["apipa"]:
            findings.append(_finding("apipa", f"{i['name']}: IP APIPA 169.254.x.x (sin DHCP).", adapter=i["name"]))
        elif i["state"] == "UP" and not i["ipv4"]:
            findings.append(_finding("no_ipv4", f"{i['name']}: UP pero sin IPv4.", adapter=i["name"]))
        elif i["state"] == "DOWN":
            findings.append(_finding("iface_down", f"{i['name']}: interfaz DOWN.", adapter=i["name"]))
    summary = f"ip addr: {len(ifaces)} interfaz(es). " + (
        " ".join(f["detail"] for f in findings) if findings else "Sin hallazgos anómalos."
    )
    obs = [{"observation_type": "ip_addr_interface", "target": i["name"], "result": i} for i in ifaces]
    return ParsedOutput("ip_addr", {"interfaces": ifaces}, findings, summary, obs)


def parse_ip_route(raw: str) -> ParsedOutput:
    routes: list[dict[str, Any]] = []
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        r: dict[str, Any] = {"destination": parts[0], "via": None, "dev": None}
        for key in ("via", "dev", "src"):
            if key in parts and parts.index(key) + 1 < len(parts):
                r[key] = parts[parts.index(key) + 1]
        routes.append(r)
    if not routes:
        raise UnsupportedCommandError("no se reconoció salida de `ip route`")
    defaults = [r for r in routes if r["destination"] == "default"]
    findings = [] if defaults else [_finding("no_default_route", "No hay ruta por defecto (default via ...).")]
    summary = "ip route: " + (
        "ruta por defecto vía " + ", ".join(f"{r['via']} ({r['dev']})" for r in defaults)
        if defaults
        else findings[0]["detail"]
    )
    obs = [{"observation_type": "ip_route", "target": r["destination"], "result": r} for r in routes]
    return ParsedOutput(
        "ip_route", {"routes": routes, "default_gateways": [r["via"] for r in defaults]}, findings, summary, obs
    )


# ---------------------------------------------------------------- ping
_WIN_STATS = re.compile(
    r"(?:sent|enviados)\s*=\s*(\d+),\s*(?:received|recibidos)\s*=\s*(\d+),\s*(?:lost|perdidos)\s*=\s*(\d+)"
)
_WIN_RTT = re.compile(
    r"(?:minimum|minimo)\s*=\s*(\d+)ms,\s*(?:maximum|maximo)\s*=\s*(\d+)ms,\s*(?:average|media)\s*=\s*(\d+)ms"
)
_LIN_STATS = re.compile(
    r"(\d+)\s+(?:packets transmitted|paquetes transmitidos),\s*(\d+)\s+(?:packets )?(?:received|recibidos)"
)
_LIN_RTT = re.compile(r"min/avg/max(?:/\w+)?\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)")
_UNREACH = (
    "destination host unreachable",
    "host de destino inaccesible",
    "network is unreachable",
    "request timed out",
    "tiempo de espera agotado",
)


def parse_ping(raw: str, command: str = "") -> ParsedOutput:
    t = _norm(raw)
    sent = received = None
    rtt: tuple[float, float, float] | None = None
    if m := _WIN_STATS.search(t):
        sent, received = int(m.group(1)), int(m.group(2))
        if r := _WIN_RTT.search(t):
            rtt = (float(r.group(1)), float(r.group(3)), float(r.group(2)))
    elif m := _LIN_STATS.search(t):
        sent, received = int(m.group(1)), int(m.group(2))
        if r := _LIN_RTT.search(t):
            rtt = (float(r.group(1)), float(r.group(2)), float(r.group(3)))
    if sent is None or received is None:
        raise UnsupportedCommandError("no se reconoció el resumen de ping (paquetes enviados/recibidos)")
    loss = round(100 * (sent - received) / sent, 1) if sent else 100.0
    target = None
    if m := re.search(r"(?:pinging|haciendo ping a)\s+(\S+)", t):
        target = m.group(1)
    elif m := re.search(r"^ping\s+(\S+)", t, re.M):
        target = m.group(1)
    if not target and command.split():
        target = command.split()[-1]
    result: dict[str, Any] = {"target": target, "sent": sent, "received": received, "loss_pct": loss}
    if rtt:
        result.update(min_ms=rtt[0], avg_ms=rtt[1], max_ms=rtt[2])
    findings: list[dict[str, str]] = []
    if received == 0:
        findings.append(_finding("no_reply", f"Sin respuesta de {target} (100% de pérdida)."))
    elif loss > 0:
        findings.append(_finding("packet_loss", f"Pérdida parcial hacia {target}: {loss}%."))
    if any(u in t for u in _UNREACH):
        findings.append(_finding("unreachable", "La salida indica host inaccesible o tiempo de espera agotado."))
    if rtt and rtt[1] > 100:
        findings.append(_finding("high_latency", f"Latencia promedio alta: {rtt[1]} ms."))
    summary = f"ping {target}: {received}/{sent} respuestas, {loss}% pérdida" + (
        f", promedio {rtt[1]} ms. " if rtt else ". "
    )
    summary += " ".join(f["detail"] for f in findings)
    obs = [{"observation_type": "ping", "target": target, "result": result, "unit": "ms"}]
    return ParsedOutput("ping", result, findings, summary.strip(), obs)


# ---------------------------------------------------------------- nslookup
_TIMEOUT = ("timed out", "timeout", "no servers could be reached", "tiempo de espera", "se agoto el tiempo")
_NX = ("non-existent domain", "nxdomain", "no existe el dominio", "no existe ese dominio")


def parse_nslookup(raw: str, command: str = "") -> ParsedOutput:
    lines = raw.splitlines()
    t = _norm(raw)
    name_idx = next((i for i, ln in enumerate(lines) if re.match(r"^\s*(name|nombre):", _norm(ln))), None)
    head = "\n".join(lines[:name_idx] if name_idx is not None else lines)
    server = (_IPV4.findall(head) or [None])[0]
    addresses = _IPV4.findall("\n".join(lines[name_idx:])) if name_idx is not None else []
    target = None
    if name_idx is not None:
        target = lines[name_idx].split(":", 1)[1].strip()
    elif command.split():
        target = command.split()[1] if len(command.split()) > 1 else None
    if any(x in t for x in _NX):
        status = "nxdomain"
    elif any(x in t for x in _TIMEOUT) and not addresses:
        status = "timeout"
    elif addresses:
        status = "resolved"
    else:
        raise UnsupportedCommandError("no se reconoció la salida de nslookup")
    result = {"query": target, "status": status, "server": server, "addresses": addresses}
    detail = {
        "resolved": f"{target} resuelve a {', '.join(addresses)} (servidor DNS {server}).",
        "nxdomain": f"El dominio {target or ''} no existe (NXDOMAIN) según el servidor {server}.",
        "timeout": f"El servidor DNS {server or ''} no respondió (timeout).",
    }[status]
    findings = [] if status == "resolved" else [_finding(status, detail)]
    obs = [{"observation_type": "nslookup", "target": target, "result": result}]
    return ParsedOutput("nslookup", result, findings, "nslookup: " + detail, obs)


# ---------------------------------------------------------------- dispatcher
def parse_command_output(os_name: str, command: str, raw_output: str) -> ParsedOutput:
    c = _norm(command).strip()
    if "ipconfig" in c:
        return parse_ipconfig(raw_output)
    if re.search(r"\bip\s+(-\w+\s+)*(a|addr|address)\b", c):
        return parse_ip_addr(raw_output)
    if re.search(r"\bip\s+(-\w+\s+)*(r|route)\b", c):
        return parse_ip_route(raw_output)
    if c.startswith("ping") or " ping" in c:
        return parse_ping(raw_output, command)
    if "nslookup" in c:
        return parse_nslookup(raw_output, command)
    raise UnsupportedCommandError(
        f"comando no soportado ({command!r}, {os_name}). Soportados: ipconfig /all, ip addr, ip route, ping, nslookup"
    )
