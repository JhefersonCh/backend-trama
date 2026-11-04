#!/usr/bin/env python
# ruff: noqa: E501
"""Retrieval quality eval against the running API (POST /api/knowledge/search).

~20 lay-Spanish queries; a chunk is RELEVANT when its text matches the query's `kw` regex (bilingual, so English
chunks count). Metrics: hit@3, MRR@10, score stats relevant vs not, share of standards in the top 3 (crowding),
share of English hits in the top 3 (cross-lingual). Usage: uv run python scripts/eval_retrieval.py [--api URL] [-v] [--json out]
"""

import argparse
import json
import re
import statistics as st

import httpx

Q = [
    ("se me fue el wifi", r"wi-?fi|wlan|wireless|inal[aá]mbric"),
    ("el módem tiene una luz roja", r"luz|led\b|light|indicator|m[oó]dem|modem"),
    ("mi PC tiene 169.254 y no navega", r"169\.254|apipa|link-local"),
    ("las páginas no cargan pero WhatsApp sí", r"\bdns\b|resoluci[oó]n de nombres|name resolution|domain name"),
    ("internet muy lento en la noche", r"congesti|saturaci|interferen|lent[oi]|slow|bandwidth|throughput|latency"),
    (
        "no aparece mi red wifi",
        r"ssid|not (?:appear|visible|listed|found)|no aparece|no se detect|oculta|hidden|adaptador|adapter",
    ),
    ("dice dirección IP duplicada", r"duplicate|duplicad|conflict|arp"),
    ("el cable de red dice desconectado", r"cable|disconnected|desconectad|link (?:down|status)|enlace"),
    ("no me deja conectar, contraseña incorrecta", r"contrase|password|passphrase|clave|authenticat|autentic|wpa"),
    ("se cae el internet cada rato", r"intermit|drops?\b|se cae|disconnect|desconex|interferen|lease|arrendamiento"),
    (
        "tengo wifi pero no tengo internet",
        r"gateway|puerta de enlace|\bdns\b|ping|dhcp|sin internet|no internet|limited connectivity",
    ),
    ("el computador no obtiene dirección IP automáticamente", r"\bdhcp\b"),
    ("cómo sé si el problema es el router o mi computador", r"ping|gateway|puerta de enlace|capas|layer|traceroute"),
    ("no abre ninguna página pero el ping funciona", r"\bdns\b|resoluci[oó]n de nombres|name resolution"),
    (
        "mi celular se conecta al wifi pero no tiene internet",
        r"dhcp|\bdns\b|gateway|puerta de enlace|captive|portal|wi-?fi",
    ),
    (
        "la señal del wifi es muy débil en mi cuarto",
        r"se[ñn]al|signal|rssi|interferen|cobertura|coverage|distance|distancia",
    ),
    ("quiero saber si mi wifi es seguro", r"wpa|security|seguridad|wlan"),
    (
        "el internet va lento solo en un computador",
        r"adaptador|adapter|driver|proxy|duplex|slow|lent[oa]|proceso|malware|virus",
    ),
    ("me sale error de DNS servidor no encontrado", r"\bdns\b|resolver|nslookup|name server|servidor de nombres"),
    ("cómo configuro una IP fija en Ubuntu", r"netplan|static|est[aá]tica|/etc/netplan"),
    (
        "qué significa destino inalcanzable",
        r"destination unreachable|destino inalcanzable|inalcanzable|icmp|unreachable",
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    rel_s: list[float] = []
    irr_s: list[float] = []
    hits3 = rr = std3 = en3 = tot3 = 0
    rows = []
    with httpx.Client(base_url=a.api.rstrip("/"), timeout=120) as c:
        docs = {d["id"]: d for d in c.get("/api/knowledge/documents").json()}
        for q, kw in Q:
            res = c.post("/api/knowledge/search", json={"query": q, "top_k": 10}).json()
            rx = re.compile(kw, re.I)
            flags = [bool(rx.search(h["content"])) for h in res]
            first = next((i for i, f in enumerate(flags) if f), None)
            hits3 += first is not None and first < 3
            rr += 1 / (first + 1) if first is not None and first < 10 else 0
            for h, f in zip(res, flags, strict=True):
                (rel_s if f else irr_s).append(h["score"])
            for h in res[:3]:
                d = docs.get(h["document_id"], {})
                tot3 += 1
                std3 += d.get("document_type") == "standard"
                en3 += d.get("language") == "en"
            top = res[0] if res else {}
            rows.append(
                {
                    "q": q,
                    "first_rel": None if first is None else first + 1,
                    "top": top.get("title"),
                    "top_score": round(top.get("score", 0), 3),
                    "section": top.get("section"),
                }
            )
            if a.v:
                mark = "HIT " if first is not None and first < 3 else "MISS"
                print(
                    f"{mark} r={first if first is None else first + 1!s:>4} {top.get('score', 0):.3f} {q[:44]:44} -> {top.get('title', '')[:40]} | {(top.get('section') or '')[:40]}"
                )
    n = len(Q)
    out = {
        "n": n,
        "hit@3": round(hits3 / n, 3),
        "mrr@10": round(rr / n, 3),
        "score_relevant": {
            "mean": round(st.mean(rel_s), 3) if rel_s else None,
            "median": round(st.median(rel_s), 3) if rel_s else None,
            "n": len(rel_s),
        },
        "score_irrelevant": {
            "mean": round(st.mean(irr_s), 3) if irr_s else None,
            "median": round(st.median(irr_s), 3) if irr_s else None,
            "n": len(irr_s),
        },
        "top3_standard_share": round(std3 / tot3, 3),
        "top3_english_share": round(en3 / tot3, 3),
        "docs": len(docs),
        "chunks": sum(d["chunk_count"] for d in docs.values()),
    }
    print(json.dumps(out, ensure_ascii=False))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"summary": out, "rows": rows}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
