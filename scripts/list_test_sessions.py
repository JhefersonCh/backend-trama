#!/usr/bin/env python
# ruff: noqa: E501
"""List (never delete) sessions that look created by automated tests.

Usage: uv run python scripts/list_test_sessions.py [--api http://localhost:8000]
"""

import argparse
import re

import httpx

_TITLES = re.compile(
    r"^\W*(hola\W*|hola, mi computador est[aá] conectado al wi-?fi.*|mi pc tiene 169\.254.*|las p[aá]ginas no cargan.*|hola, se me fue el wifi.*)$",
    re.I,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    base = ap.parse_args().api.rstrip("/")
    with httpx.Client(base_url=base, timeout=60) as c:
        sessions = c.get("/api/sessions", params={"limit": 200, "include_archived": True}).json()
        found = 0
        for s in sessions:
            n = len(c.get(f"/api/sessions/{s['id']}/messages").json())
            if _TITLES.match(s.get("title") or "") or n <= 1:
                found += 1
                print(f"{s['id']}  {s.get('started_at', '?')[:16]}  msgs={n:<3} {s.get('title')!r}")
    print(f"\n{found} candidate(s) of {len(sessions)} sessions (report only, nothing deleted)")


if __name__ == "__main__":
    main()
