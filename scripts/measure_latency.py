#!/usr/bin/env python3
"""Measure main-path latency for agent.py.

Per plan/bedrock-integration.md § 5.5. Runs `python3 agent.py -a <prompt>`
N times, measures wall-clock, and parses the most recent session log for
per-turn latency derived from `--- Turn` markers.

Usage:
    scripts/measure_latency.py baseline/simple.stdout.log    # re-run same prompt
    scripts/measure_latency.py --prompt "say hi"             # ad-hoc prompt

Prints median and p95 for wall-clock and per-turn latency.
"""

import glob
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_RUNS = 5
REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_LOG_GLOB = ".agent/history/session_*.log"

_USER_LINE_RE = re.compile(r"^You:\s*(.+)$")
# Session-log turn marker — supports both full-date and HH:MM:SS prefixes.
_TURN_MARKER_RE = re.compile(
    r"^(?P<ts>(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+.*---\s*Turn\s+\d+",
    re.IGNORECASE,
)


def _extract_prompt_from_log(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _USER_LINE_RE.search(line.strip())
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return None


def _parse_ts(s: str) -> float | None:
    """Parse 'HH:MM:SS[.fff]' or 'YYYY-MM-DD HH:MM:SS[.fff]'.

    Returns seconds-since-midnight (for HH:MM:SS-only) or epoch seconds
    (for full-date). Caller only uses differences, so mixing is OK.
    """
    s = s.strip().replace(",", ".")
    try:
        if len(s) >= 10 and s[4] == "-":
            t = time.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            frac_start = 19
            base = time.mktime(t)
        else:
            t = time.strptime(s[:8], "%H:%M:%S")
            frac_start = 8
            base = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
        frac = 0.0
        if len(s) > frac_start + 1 and s[frac_start] == ".":
            try:
                frac = float("0." + s[frac_start + 1 :])
            except ValueError:
                frac = 0.0
        return base + frac
    except ValueError:
        return None


def _per_turn_latencies_from_log(path: str) -> list[float]:
    markers: list[float] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _TURN_MARKER_RE.search(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if ts is not None:
                        markers.append(ts)
    except OSError:
        return []
    if len(markers) < 2:
        return []
    return [b - a for a, b in zip(markers[:-1], markers[1:])]


def _newest_session_log() -> str | None:
    matches = sorted(glob.glob(str(REPO_ROOT / SESSION_LOG_GLOB)), key=os.path.getmtime)
    return matches[-1] if matches else None


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _run_once(prompt: str) -> tuple[float, list[float]]:
    before = _newest_session_log()
    t0 = time.monotonic()
    subprocess.run(
        ["python3", "agent.py", "-a", prompt],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    wall = time.monotonic() - t0
    after = _newest_session_log()
    turn_latencies: list[float] = []
    if after and after != before:
        turn_latencies = _per_turn_latencies_from_log(after)
    return wall, turn_latencies


def main(argv: list[str]) -> int:
    runs = DEFAULT_RUNS
    prompt: str | None = None
    positional_runs_idx = -1

    if len(argv) >= 2 and argv[1] == "--prompt":
        if len(argv) < 3:
            print("usage: measure_latency.py --prompt <text> [runs]", file=sys.stderr)
            return 2
        prompt = argv[2]
        positional_runs_idx = 3
    elif len(argv) >= 2 and argv[1].endswith(".stdout.log"):
        prompt = _extract_prompt_from_log(argv[1])
        if not prompt:
            print(f"could not extract prompt from {argv[1]}", file=sys.stderr)
            return 2
        positional_runs_idx = 2
    else:
        print(
            "usage: measure_latency.py <baseline/X.stdout.log | --prompt TEXT> [runs]",
            file=sys.stderr,
        )
        return 2

    if positional_runs_idx >= 0 and len(argv) > positional_runs_idx and argv[positional_runs_idx].isdigit():
        runs = int(argv[positional_runs_idx])

    print(f"# measure_latency: prompt={prompt!r} runs={runs}")

    walls: list[float] = []
    all_turn_latencies: list[float] = []
    for i in range(runs):
        wall, turns = _run_once(prompt)
        walls.append(wall)
        all_turn_latencies.extend(turns)
        per_turn_str = ""
        if turns:
            per_turn_str = f" p50_turn={statistics.median(turns):.2f}s"
        print(f"  run {i + 1}/{runs}: wall={wall:.2f}s turns={len(turns)}" + per_turn_str)

    print()
    print("wall-clock (s):")
    print(f"  median: {statistics.median(walls):.3f}")
    print(f"  p95   : {_pct(walls, 95):.3f}")
    if all_turn_latencies:
        print("per-turn (s):")
        print(f"  median: {statistics.median(all_turn_latencies):.3f}")
        print(f"  p95   : {_pct(all_turn_latencies, 95):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
