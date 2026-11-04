#!/usr/bin/env python
"""Upload the seed corpus in knowledge_base/ through the running API.

Usage: uv run python scripts/ingest_seed.py [--api http://localhost:8000]
Re-running is safe: files already ingested answer 409 and are reported as skipped.
"""

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent / "knowledge_base"

GUIDE = {
    "publisher": "TRAMA (redacción propia)",
    "document_type": "guide",
    "license": "Contenido propio del proyecto",
    "language": "es",
    "version": "1.0",
    "data_origin": "public_doc",
    "is_synthetic": "false",
}
FILES = {
    "diagnostic_cases/net_cases.jsonl": {
        **GUIDE,
        "title": "Casos de diagnóstico NET-001 a NET-015",
        "document_type": "diagnostic_case",
        "source_uri": "internal://trama/diagnostic_cases/net_cases",
        "data_origin": "synthetic",
        "is_synthetic": "true",
    },
    "diagnostico_por_capas.md": {
        **GUIDE,
        "title": "Diagnóstico de red por capas",
        "source_uri": "https://www.rfc-editor.org/rfc/rfc1122",
    },
    "dhcp_apipa.md": {
        **GUIDE,
        "title": "DHCP y direcciones APIPA",
        "source_uri": "https://www.rfc-editor.org/rfc/rfc2131",
    },
    "dns_resolucion.md": {
        **GUIDE,
        "title": "DNS: resolución de nombres",
        "source_uri": "https://www.rfc-editor.org/rfc/rfc1035",
    },
    "luces_modem_router.md": {
        **GUIDE,
        "title": "Luces del módem y del router: qué significan",
        "source_uri": "internal://trama/guides/luces_modem_router",
    },
    "wifi_senal_y_congestion.md": {
        **GUIDE,
        "title": "Wi-Fi: señal, interferencia y congestión",
        "source_uri": "https://www.ieee802.org/11/",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    base = ap.parse_args().api.rstrip("/")
    failed = 0
    with httpx.Client(base_url=base, timeout=300) as c:
        for rel, meta in FILES.items():
            path = ROOT / rel
            r = c.post("/api/knowledge/documents", data=meta, files={"file": (path.name, path.read_bytes())})
            if r.status_code == 201:
                b = r.json()
                print(
                    f"OK    {rel}: {b['chunks']} chunks, {b['embedded']} embedded"
                    + (f" [{b['warning']}]" if b["warning"] else "")
                )
            elif r.status_code == 409:
                print(f"SKIP  {rel}: already ingested")
            else:
                failed += 1
                print(f"FAIL  {rel}: {r.status_code} {r.text}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
