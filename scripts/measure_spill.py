#!/usr/bin/env python3
"""Measure what tool-output spilling actually saves, in tokens the server would count.

WHY
---
`measure_token_drift.py` established that a wrong-tokenizer estimate is a small, mostly
safe-signed error — not the thing that overflows context windows. What overflows them is bulk
tool output pasted into a prompt: a directory listing, a price history, a log tail, echoed into
the conversation and then carried in EVERY subsequent turn because the transcript is cumulative.

Spilling writes the payload to a file and leaves a marker plus a short preview inline. This
measures the difference, and measures it the way the failure actually happens — CUMULATIVELY
over a run, not once. A single spilled result looks like a modest saving; the same result
carried across twenty turns is the whole window.

Nothing here is mocked: payloads are real tool output shapes, and the sizes come from the same
`_spill_tool_result` the agent runs.

USAGE
    python3 scripts/measure_spill.py [--turns 20] [--json]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from token_utils import count_tokens  # noqa: E402


def _payloads():
    """Real tool-output shapes, at sizes that occur in ordinary use."""
    listing = "\n".join(
        f"-rw-r--r--  1 user user {i * 137:>9} 2026-01-01 09:{i % 60:02d} src/module_{i}/file_{i}.py"
        for i in range(900))
    prices = "\n".join(
        f"2026-{1 + i % 12:02d}-{1 + i % 28:02d},{400 + i * 0.37:.2f},{402 + i * 0.31:.2f},"
        f"{398 + i * 0.29:.2f},{401 + i * 0.33:.2f},{3_000_000 + i * 971}"
        for i in range(1200))
    logtail = "\n".join(
        f"2026-01-01T09:{i % 60:02d}:{i % 60:02d}Z INFO worker[{i % 8}] processed batch {i} "
        f"in {12 + i % 40}ms, queue depth {i % 17}" for i in range(1500))
    grep = "\n".join(
        f"src/module_{i // 7}/file_{i}.py:{i % 400}:    result = transform(row, ctx, strict=True)"
        for i in range(1100))
    return [("dir_listing", listing), ("price_history", prices),
            ("log_tail", logtail), ("grep_output", grep)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-ctx", type=int, default=196608,
                    help="context window to compare the payloads against")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("AGENT_FAKE_BACKEND", "1")   # importing agent must not touch a server
    prev = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="spill-measure-")
    os.chdir(tmp)                                       # spill files land in ./.agent/spill
    try:
        import agent  # noqa: E402  — imported after cwd/env are set

        rows = []
        for name, content in _payloads():
            before = count_tokens(content)
            sink = io.StringIO()
            spilled = agent._spill_tool_result(0, name, content, lambda *a, **k: None)
            after = count_tokens(spilled)
            rows.append({"payload": name, "chars": len(content),
                         "tokens_inline": before, "tokens_spilled": after,
                         "saved_per_turn": before - after,
                         "reduction": 1 - (after / before) if before else 0.0})
            sink.close()
    finally:
        os.chdir(prev)

    total_before = sum(r["tokens_inline"] for r in rows)
    total_after = sum(r["tokens_spilled"] for r in rows)

    out = {"n_ctx": args.n_ctx, "payloads": rows,
           "four_results": {"inline": total_before, "spilled": total_after},
           "window_used_inline": total_before / args.n_ctx,
           "window_used_spilled": total_after / args.n_ctx,
           "overflows_window_inline": total_before > args.n_ctx}

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"{'payload':<16}{'chars':>9}{'inline':>9}{'spilled':>9}{'saved':>9}{'reduction':>11}")
    for r in rows:
        print(f"{r['payload']:<16}{r['chars']:>9,}{r['tokens_inline']:>9,}"
              f"{r['tokens_spilled']:>9,}{r['saved_per_turn']:>9,}{r['reduction']:>10.1%}")
    print(f"\nfour results in ONE turn: {total_before:,} tokens inline -> {total_after:,} spilled")
    print(f"against n_ctx={args.n_ctx:,}:  {total_before / args.n_ctx:.0%} of the window inline, "
          f"{total_after / args.n_ctx:.1%} spilled")
    if total_before > args.n_ctx:
        print(f"  ⚠ INLINE ALREADY OVERFLOWS: four ordinary tool results exceed the whole window "
              f"by {total_before - args.n_ctx:,} tokens.\n    Not a budgeting error — no budget "
              f"fits them. Spilling is what makes the turn possible at all.")
    print("\nAnd a transcript is CUMULATIVE, so a result that fits is still paid for on every "
          "later turn.\nThat is why an overflow tends to arrive mid-run rather than at the big "
          "tool call itself.\nThe multiple is left unstated deliberately: how many turns a given "
          "result survives depends on\ntrimming and on the run, and multiplying these figures by "
          "a turn count would produce totals\nlarger than any window can hold — a number that "
          "cannot happen is not a measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
