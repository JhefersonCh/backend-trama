#!/usr/bin/env python
"""Download the reference corpus into knowledge_base/sources/<id>/raw.<ext> and write meta.json.

raw.* is git-ignored (copyrighted sources are not redistributed); meta.json is committed.
Usage: uv run python scripts/fetch_sources.py [--only id,id] [--force]
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent / "knowledge_base" / "sources"
UA = "TRAMA-academic-rag/0.1"
RFC = "https://www.rfc-editor.org/rfc/rfc{}.txt"
IETF = "IETF Trust (RFC: copia permitida con aviso de copyright)"
UBUNTU = "CC BY-SA 3.0 (Canonical; aviso de licencia no verificable en la pagina)"
COPY = "Copyright del editor; uso académico local, no redistribuido"
MS_RAW = "https://raw.githubusercontent.com/MicrosoftDocs/SupportArticles-docs/main/support/windows-client/networking/"


def rfc(n, title, version=None):
    return dict(
        id=f"rfc{n}",
        url=RFC.format(n),
        ext="txt",
        title=f"RFC {n}: {title}",
        publisher="IETF",
        language="en",
        license=IETF,
        document_type="standard",
        version=version,
        origin_uri=f"https://www.rfc-editor.org/rfc/rfc{n}",
    )


SOURCES = [
    dict(
        id="cisco_tcpip",
        url="https://www.cisco.com/en/US/docs/internetworking/troubleshooting/guide/tr1907.html",
        ext="html",
        title="Troubleshooting TCP/IP",
        publisher="Cisco",
        language="en",
        license=COPY,
        document_type="troubleshooting_guide",
    ),
    dict(
        id="cisco_dhcp",
        url="https://www.cisco.com/c/en/us/support/docs/ip/dynamic-address-allocation-resolution/27470-100.html",
        ext="html",
        title="Troubleshoot DHCP",
        publisher="Cisco",
        language="en",
        license=COPY,
        document_type="troubleshooting_guide",
    ),
    dict(
        id="cisco_ping_traceroute",
        url="https://www.cisco.com/c/en/us/support/docs/ios-nx-os-software/ios-software-releases-121-mainline/12778-ping-traceroute.html",
        ext="html",
        title="Understand the Ping and Traceroute Commands",
        publisher="Cisco",
        language="en",
        license=COPY,
        document_type="troubleshooting_guide",
    ),
    dict(
        id="cisco_devnet_iotod",
        url="https://developer.cisco.com/docs/iotod/troubleshooting-issues/",
        ext="html",
        title="Cisco DevNet IoT OD: Troubleshooting issues",
        publisher="Cisco DevNet",
        language="en",
        license=COPY,
        document_type="troubleshooting_guide",
    ),
    dict(
        id="ms_wireless_en",
        url="https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/wireless-network-connectivity-issues-troubleshooting",
        ext="html",
        title="Advanced troubleshooting for wireless network connectivity",
        publisher="Microsoft Learn",
        language="en",
        license="Microsoft Learn docs: CC BY 4.0 (MicrosoftDocs/SupportArticles-docs)",
        document_type="troubleshooting_guide",
        alt_url=MS_RAW + "wireless-network-connectivity-issues-troubleshooting.md",
        alt_ext="md",
    ),
    dict(
        id="ms_wifi_es",
        url="https://support.microsoft.com/es-es/windows/experience/connectivity-networking/fix-wi-fi-connection-issues-in-windows",
        ext="html",
        title="Solucionar problemas de conexión Wi-Fi en Windows",
        publisher="Microsoft Support",
        language="es",
        license=COPY,
        document_type="os_guide",
    ),
    *[
        dict(
            id=f"ubuntu_{k}",
            url=f"https://ubuntu.com/server/docs/{path}/",
            ext="html",
            title=f"Ubuntu Server: {t}",
            publisher="Canonical",
            language="en",
            license=UBUNTU,
            document_type="os_guide",
        )
        for k, path, t in [
            ("key_concepts", "explanation/networking/networking-key-concepts", "Networking key concepts"),
            ("configuring_networks", "explanation/networking/configuring-networks", "Configuring networks"),
            ("about_dhcp", "explanation/networking/about-dhcp", "About DHCP"),
        ]
    ],
    dict(
        id="ubuntu_netplan",
        url="https://ubuntu.com/server/docs/explanation/networking/about-netplan/",
        ext="html",
        title="Ubuntu Server: About Netplan",
        publisher="Canonical",
        language="en",
        license=UBUNTU,
        document_type="os_guide",
    ),
    dict(
        id="nist_sp800_153",
        url="https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-153.pdf",
        ext="pdf",
        title="NIST SP 800-153: Guidelines for Securing Wireless Local Area Networks (WLANs)",
        publisher="NIST",
        language="en",
        license="Dominio público (gobierno de EE. UU.)",
        document_type="security_guideline",
        version="2012-02",
        origin_uri="https://doi.org/10.6028/NIST.SP.800-153",
    ),
    rfc(1035, "Domain Names - Implementation and Specification"),
    rfc(2131, "Dynamic Host Configuration Protocol"),
    rfc(9293, "Transmission Control Protocol (TCP)"),
    rfc(792, "Internet Control Message Protocol"),
    rfc(8200, "Internet Protocol, Version 6 (IPv6) Specification"),
    rfc(3927, "Dynamic Configuration of IPv4 Link-Local Addresses"),
]


def fetch(c: httpx.Client, s: dict, force: bool) -> str:
    d = ROOT / s["id"]
    raw = d / f"raw.{s['ext']}"
    if raw.exists() and not force:
        return "have"
    d.mkdir(parents=True, exist_ok=True)
    tries = [(s["url"], s["ext"])] + ([(s["alt_url"], s["alt_ext"])] if s.get("alt_url") else [])
    err = ""
    for url, ext in tries:
        try:
            r = c.get(url)
            if r.status_code == 200 and len(r.content) > 2000:
                raw = d / f"raw.{ext}"
                raw.write_bytes(r.content)
                meta = {k: v for k, v in s.items() if k not in ("url", "alt_url", "alt_ext", "origin_uri", "id")}
                meta.update(
                    source_uri=s.get("origin_uri", s["url"]),
                    fetched_from=url,
                    ext=ext,
                    fetched_at=date.today().isoformat(),
                    bytes=len(r.content),
                )
                (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
                return "ok"
            err = f"HTTP {r.status_code} ({len(r.content)} bytes)"
        except httpx.HTTPError as e:
            err = str(e)
    return f"FAIL {err}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    bad = 0
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as c:
        for s in SOURCES:
            if only and s["id"] not in only:
                continue
            res = fetch(c, s, a.force)
            bad += res.startswith("FAIL")
            print(f"{s['id']:24} {res}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
