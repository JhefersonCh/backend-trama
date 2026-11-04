#!/usr/bin/env python
"""Ingest the reference corpus fetched by scripts/fetch_sources.py through the running API.

Usage: uv run python scripts/ingest_sources.py [--api http://localhost:8000] [--only id,id]
Idempotent: duplicates (same checksum) answer 409 and are skipped.
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent / "knowledge_base" / "sources"
FORM = ("title", "publisher", "source_uri", "document_type", "license", "language", "version", "publication_date")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    failed = 0
    with httpx.Client(base_url=a.api.rstrip("/"), timeout=1800) as c:
        for d in sorted(p for p in ROOT.iterdir() if (p / "meta.json").exists()):
            if only and d.name not in only:
                continue
            meta = json.loads((d / "meta.json").read_text())
            raw = d / f"raw.{meta['ext']}"
            if not raw.exists():
                print(f"MISS  {d.name}: raw file absent (run scripts/fetch_sources.py)")
                continue
            form = {k: meta[k] for k in FORM if meta.get(k)}
            form.update(data_origin="public_doc", is_synthetic="false")
            r = c.post("/api/knowledge/documents", data=form, files={"file": (raw.name, raw.read_bytes())})
            if r.status_code == 201:
                b = r.json()
                print(
                    f"OK    {d.name}: {b['chunks']} chunks, {b['embedded']} embedded"
                    + (f" [{b['warning']}]" if b["warning"] else "")
                )
            elif r.status_code == 409:
                print(f"SKIP  {d.name}: already ingested")
            else:
                failed += 1
                print(f"FAIL  {d.name}: {r.status_code} {r.text[:200]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
