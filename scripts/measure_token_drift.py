#!/usr/bin/env python3
"""Measure the context estimator's drift against the SERVED model's real token count.

WHY THIS EXISTS
---------------
``token_utils`` counts with a fixed tokenizer. The server may be running a different model
family entirely, so the estimate that sizes every context budget is a guess about someone
else's vocabulary. F3b (``agent.py:_update_token_calibration``) corrects for this at runtime
from the ``prompt_tokens`` every streamed turn reports; this script is how you find out how
big the error actually is, before and after a model swap.

Run it whenever the served model changes. A ratio far from 1.0, or a WIDE SPREAD across
content classes, is the signal that the budget needs the calibration rather than the guess.

READING THE OUTPUT
------------------
``ratio = actual / estimated``.

* ratio > 1.0 — the server counts MORE than we estimated. We UNDER-budget: this is the
  direction that overflows a context window, and the worst class sets the risk, not the mean.
* ratio < 1.0 — we over-estimate and leave usable window unused. Safe, but wasteful.

Report the SPREAD, not just the mean: a uniform ratio is a constant the budget absorbs, a
content-dependent one is a hazard that averages away on paper and bites on the worst input.

The chat template adds tokens the text estimate cannot see. This measures that separately
(a fixed preamble plus a per-message cost) instead of smearing it into the ratio, because the
two scale differently: the template cost grows with the number of MESSAGES, the tokenizer
error with their SIZE.

USAGE
-----
    python3 scripts/measure_token_drift.py [--base-url URL] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from token_utils import count_tokens  # noqa: E402

DEFAULT_BASE = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8080")

PROSE = ("The estimator is the first guess, not a permanent assumption. A tokenizer mismatch "
         "between the counter and the served model shows up as a ratio far from one, and the "
         "budget that rides on it drifts the same way on every turn. ") * 12
CODE = ('def _update_token_calibration(estimated_tokens, actual_prompt_tokens, log=None):\n'
        '    ratio = actual_prompt_tokens / max(1, estimated_tokens)\n'
        '    cal["ratio"] = (1 - ALPHA) * cal["ratio"] + ALPHA * ratio\n'
        '    return max(MIN, min(MAX, cal["ratio"]))\n') * 12
JSONISH = json.dumps([{"id": f"row-{i}", "score": 1.3 + i / 50, "interval": i * 3,
                       "misses": i % 7, "category": "reference"} for i in range(40)])
TOOLOUT = "\n".join(f"{i:>6}\t-rw-r--r--  1 user user {i * 137:>9} 2026-01-01 09:{i % 60:02d} "
                    f"path/to/file-{i}.js" for i in range(60))
CJK = "context window budget drift measurement 上下文窗口预算漂移测量 " * 20

CASES = [("prose", PROSE), ("code", CODE), ("json", JSONISH),
         ("tool_output", TOOLOUT), ("mixed_cjk", CJK)]


def prompt_tokens(base_url: str, messages: list[dict]) -> int:
    body = json.dumps({"messages": messages, "max_tokens": 1,
                       "temperature": 0, "stream": False}).encode()
    req = urllib.request.Request(f"{base_url.rstrip('/')}/v1/chat/completions", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["usage"]["prompt_tokens"]


def measure_template_overhead(base_url: str) -> tuple[int, float]:
    """Return (fixed_preamble, per_message) by fitting the two message counts we sample.

    Two points are enough for a line, and the caller sees both raw numbers in --json; the
    point is the SHAPE (fixed + k*n), not a precise k.
    """
    unit = "The estimator is the first guess. " * 4
    pts = []
    for n in (1, 33):
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": unit}
                for i in range(n)]
        if msgs[-1]["role"] == "assistant":
            msgs.append({"role": "user", "content": unit})
        est = sum(count_tokens(m["content"]) for m in msgs)
        pts.append((len(msgs), prompt_tokens(base_url, msgs) - est))
    (n1, o1), (n2, o2) = pts
    per_msg = (o2 - o1) / max(1, (n2 - n1))
    fixed = o1 - per_msg * n1
    return int(round(fixed)), per_msg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--n-ctx", type=int, default=0,
                    help="context size, to report the drift in absolute tokens")
    args = ap.parse_args()

    try:
        fixed, per_msg = measure_template_overhead(args.base_url)
    except (urllib.error.URLError, OSError) as e:
        print(f"cannot reach {args.base_url}: {e}", file=sys.stderr)
        print("This measurement REQUIRES a live server — there is no mock answer to the "
              "question 'what does the server actually count?'", file=sys.stderr)
        return 2

    rows = []
    for name, text in CASES:
        est = count_tokens(text)
        act = prompt_tokens(args.base_url, [{"role": "user", "content": text}])
        act -= fixed + per_msg          # strip template cost: compare TEXT to TEXT
        rows.append({"class": name, "estimated": est, "actual": int(round(act)),
                     "ratio": act / max(1, est)})

    ratios = [r["ratio"] for r in rows]
    mean, lo, hi = sum(ratios) / len(ratios), min(ratios), max(ratios)
    riskiest = max(rows, key=lambda r: r["ratio"])["class"]
    out = {"base_url": args.base_url, "template_fixed_tokens": fixed,
           "template_per_message_tokens": round(per_msg, 2), "classes": rows,
           "mean_ratio": round(mean, 4), "spread": [round(lo, 4), round(hi, 4)],
           "riskiest_class": riskiest,
           "under_budgets": hi > 1.0}

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"server: {args.base_url}")
    print(f"chat template: {fixed} fixed + {per_msg:.1f} per message "
          f"(the text estimate counts NEITHER)")
    print(f"{'class':<12} {'est':>7} {'actual':>7} {'ratio':>7} {'drift':>9}")
    for r in rows:
        print(f"{r['class']:<12} {r['estimated']:>7} {r['actual']:>7} "
              f"{r['ratio']:>7.3f} {(r['ratio'] - 1) * 100:>+8.1f}%")
    print(f"\nmean {mean:.3f} | spread {lo:.3f}-{hi:.3f} | riskiest class: {riskiest}")
    if hi > 1.0:
        print(f"  ⚠ '{riskiest}' UNDER-estimates by {(hi - 1) * 100:.1f}% — that is the "
              f"overflow direction; size the budget by the worst class, not the mean.")
    else:
        print("  no class under-estimates: the guess is conservative on every class sampled "
              "(safe, but it leaves window unused).")
    if args.n_ctx:
        print(f"At n_ctx={args.n_ctx:,}: {int(args.n_ctx * (mean - 1)):+,} tokens on the mean, "
              f"{int(args.n_ctx * (hi - 1)):+,} in the riskiest class.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
