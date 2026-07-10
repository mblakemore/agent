#!/usr/bin/env python3
"""S2 spike prototype — availability prober for WS7 tier routing.

Reports which model tier is currently reachable:
  claude-available  Bedrock/UCSB path healthy and under quota
  local-strong      qwen/strong local model serving (reserved; same probe as
                    local-base until the qwen deploy gets its own port)
  local-base        llama.cpp gemma main model serving
  degraded          nothing but the framework itself

Hysteresis: a tier must fail N_CONSECUTIVE (default 2) consecutive probes to
be demoted, and pass 1 to be promoted — state kept in
.agent/state/availability.json so flapping gateways don't thrash routing.

Usage:
  python3 scripts/availability_probe.py            # human-readable
  python3 scripts/availability_probe.py --json     # machine-readable
Exit code: 0 = claude-available, 1 = local tier only, 2 = degraded.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO, ".agent", "state", "availability.json")
N_CONSECUTIVE = 2

PROBES = {
    # name: (url, ok_statuses)  — 401 counts as alive-but-authed for proxies
    "cc_gateway": ("http://127.0.0.1:8788/health", {200}),
    "litellm": ("http://127.0.0.1:4000/health/readiness", {200, 401}),
    "shim": ("http://127.0.0.1:4500/health", {200}),
    "llama_main": ("http://127.0.0.1:8080/health", {200}),
    "llama_summary": ("http://127.0.0.1:8082/health", {200}),
}


def _probe(url, ok, timeout=3):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in ok
    except urllib.error.HTTPError as e:
        return e.code in ok
    except Exception:
        return False


def _bedrock_ok():
    """Claude-tier check: any healthy, under-cap entry in the bedrock store.
    Uses bedrock_store's own health data (no live call — cheap); a live
    /token-usage probe belongs in the slow path, not every PERCEIVE."""
    try:
        sys.path.insert(0, REPO)
        import bedrock_store
        entry = bedrock_store.select_entry()
        return entry is not None
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    raw = {name: _probe(url, ok) for name, (url, ok) in PROBES.items()}
    raw["bedrock_store"] = _bedrock_ok()

    # Load hysteresis state
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except Exception:
        state = {"fail_counts": {}, "tier": "degraded"}
    fails = state.get("fail_counts", {})

    for name, up in raw.items():
        fails[name] = 0 if up else fails.get(name, 0) + 1

    def _up(name):
        # up now, or failed fewer than N consecutive probes (hysteresis hold)
        return raw[name] or fails.get(name, 0) < N_CONSECUTIVE

    claude_path = raw["bedrock_store"] or _up("litellm") or _up("cc_gateway")
    local_path = _up("llama_main")

    if claude_path and (raw["bedrock_store"] or raw["litellm"] or raw["cc_gateway"]):
        tier = "claude-available"
    elif local_path:
        tier = "local-base"  # local-strong needs its own qwen port probe
    else:
        tier = "degraded"

    state = {"fail_counts": fails, "tier": tier, "raw": raw,
             "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    if args.json:
        print(json.dumps(state))
    else:
        print(f"tier: {tier}")
        for name, up in raw.items():
            print(f"  {name:14s} {'UP' if up else 'down'}"
                  f"{'' if up else f' (fails={fails[name]})'}")
    return 0 if tier == "claude-available" else (1 if tier != "degraded" else 2)


if __name__ == "__main__":
    sys.exit(main())
