#!/usr/bin/env python3
"""Verify every PromQL expression in a Grafana dashboard JSON resolves.

For each panel target's `expr`, runs `/api/v1/query` against Prometheus and
reports the number of series returned. Zero series is a warning (panels may
filter to a not-yet-active instance/job); a 400 from Prometheus (syntactically
invalid query, or unknown function) exits 1.

Usage:
    python3 scripts/verify_dashboard.py [--prom-url URL] dashboards/agentpy-fleet.json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
import urllib.error


def extract_targets(dashboard: dict) -> list[tuple[str, str]]:
    """Return [(panel_title, expr), ...] from a Grafana dashboard."""
    out: list[tuple[str, str]] = []
    for panel in dashboard.get("panels", []) or []:
        title = panel.get("title", "<untitled>")
        for tgt in panel.get("targets", []) or []:
            expr = tgt.get("expr")
            if expr:
                out.append((title, expr))
        # Some dashboards nest panels under rows
        for sub in panel.get("panels", []) or []:
            sub_title = sub.get("title", "<untitled>")
            for tgt in sub.get("targets", []) or []:
                expr = tgt.get("expr")
                if expr:
                    out.append((sub_title, expr))
    return out


def query_prom(prom_url: str, expr: str, timeout: float = 5.0) -> tuple[int, int, str]:
    """Return (http_status, series_count, error_message)."""
    qs = urllib.parse.urlencode({"query": expr})
    url = f"{prom_url.rstrip('/')}/api/v1/query?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            err = body.get("error", str(exc))
        except Exception:
            err = str(exc)
        return exc.code, 0, err
    except Exception as exc:  # network errors, JSON errors
        return 0, 0, f"{type(exc).__name__}: {exc}"

    if payload.get("status") != "success":
        return status, 0, payload.get("error", "non-success status")
    result = payload.get("data", {}).get("result", []) or []
    return status, len(result), ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dashboard", help="Path to Grafana dashboard JSON")
    parser.add_argument(
        "--prom-url", default="http://localhost:9090", help="Prometheus base URL"
    )
    args = parser.parse_args(argv)

    with open(args.dashboard, "r", encoding="utf-8") as fh:
        dashboard = json.load(fh)

    targets = extract_targets(dashboard)
    if not targets:
        print("no panel targets found", file=sys.stderr)
        return 1

    fail = False
    for title, expr in targets:
        status, count, err = query_prom(args.prom_url, expr)
        if status == 400 or (err and status != 200):
            print(f"FAIL [{title}] {expr!r} -> {status} {err}")
            fail = True
        elif count == 0:
            print(f"WARN [{title}] {expr!r} -> 0 series")
        else:
            print(f"OK   [{title}] {expr!r} -> {count} series")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
