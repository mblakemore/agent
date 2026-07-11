#!/usr/bin/env python3
"""Replay suite v0 — local-model reasoning benchmark (WS10.h, spike S3).

Replays 5 real, closed CICD issues against their pinned base commits and
scores whether the agent solves them, with NO GitHub involvement: success is
each issue's own measurement command passing inside the worktree.

Modes:
  --dry-run (default)  Validate mechanics only: create worktrees at base
                       SHAs, seed prompts, run each measurement command and
                       assert it FAILS at base (the gap is real + machine-
                       detectable). No LLM involved.
  --live               Also run the agent (builder-only, no gh) in each
                       worktree, then score: measurement passes = success.
                       Repeat with --k N (pass@k — output is nondeterministic
                       even at temp 0; see plan/DECISIONS.md).

Usage:
  python3 scripts/replay_suite.py                     # dry-run, all issues
  python3 scripts/replay_suite.py --issues M1,P1      # subset
  python3 scripts/replay_suite.py --live --k 3 --turn-cap 60
  python3 scripts/replay_suite.py --live --agent-arg=--backend-main=bedrock

Results: temp/replay/results-<stamp>.jsonl (one row per issue×rep) + table.

Checkouts are SHARED CLONES under temp/replay/, not git worktrees — two
load-bearing reasons discovered during positive-control validation:
  1. CICD itself ran from clones (temp/<stamp>/repo). Running from a
     worktree adds the issue-1012 cwd trap to EVERY issue, which made M1's
     criterion unachievable even at its own historical fix commit.
  2. The 'temp' path segment is DELIBERATE — M1's baseline failure only
     reproduces when the checkout path contains a temp/ component.
H1 is the exception: its bug IS "pytest invoked from a worktree", so its
measurement runs inside a fresh inner worktree of the clone at HEAD
(created fresh at scoring time so the agent's local commits are included).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAY_ROOT = os.path.join(REPO, "temp", "replay")

SEED_HEADER = """LOCAL REPLAY MODE — read carefully:
You are working directly in the current directory, which is already an
isolated git worktree of this repository at a historical state. Do NOT use
gh. Do NOT git push. Do NOT create worktrees, branches, PRs, or issues.
Implement the improvement described below, write/repair tests as the plan
specifies, verify with the measurement command, then git commit locally and
call end_cycle. Success is measured ONLY by the measurement command passing.

"""

# Each issue: base = git SHA the worktree is pinned to; measure = shell
# command run with cwd=worktree, exit 0 = solved. All five FAIL at base
# (dry-run asserts this) and passed after the original human/CICD fix.
ISSUES = {
    "E1": {
        "title": "1013 — remove dead _find_definitions helper",
        "base": "02c0a8a",
        "seed_plan": "CICD/improvements/1013-remove-dead-find-definitions.md",
        "measure": ("python3 -m pytest tests/test_find_symbol.py -q "
                    "&& ! grep -qE 'def _find_definitions\\(' tools/find_symbol.py"),
        "difficulty": "easy",
    },
    "M1": {
        "title": "1011 — _is_excluded relative-to-root matching",
        "base": "eeb9249",
        "seed_plan": "CICD/improvements/1011-find-symbol-excludes-relative.md",
        "measure": ("python3 -m pytest tests/test_find_symbol.py::TestFindSymbolAC2 "
                    "tests/test_find_symbol.py::TestFindSymbolAC3 -q"),
        "difficulty": "medium",
    },
    "M2": {
        "title": "1026 — task_tracker persistent flag plumbing",
        "base": "3011167",
        "seed_plan": "CICD/improvements/1026-task-tracker-persistent-flag.md",
        "measure": ("grep -q 'persistent' tools/task_tracker.py "
                    "&& grep -q 'persistent' tests/test_task_tracker.py "
                    "&& python3 -m pytest tests/test_task_tracker.py -q"),
        "difficulty": "medium",
    },
    "H1": {
        "title": "1012 — conftest find_symbol cwd under worktree",
        "base": "0362a69",
        "seed_plan": "CICD/improvements/1012-conftest-find-symbol-cwd-worktree.md",
        "measure": "python3 -m pytest tests/test_find_symbol.py -q",
        # The bug is literally "pytest from a worktree cwd" — measure there.
        "measure_in_inner_worktree": True,
        "difficulty": "hard",
    },
    "P1": {
        "title": "run104/#289 — guardrail test coverage (mock-pattern trap)",
        "base": "daf4058",
        "seed_text": (
            "Issue #289 — Test Coverage: guardrails in agent.py's tool "
            "validation. Add pytest tests to tests/test_cicd_guards.py "
            "covering: (1) `_validate_tool_call` handling of a `file` call "
            "with a missing `path` argument (name the test "
            "test_tool_validation_missing_path_v2); (2) CICD PR-number "
            "capture from `gh pr create` output (tests named "
            "test_cicd_pr_capture_*): a successful create whose result "
            "contains a real pull/NNN URL records the PR number, and a "
            "result without a URL records none. HINT that the original "
            "builder burned 44 turns ignoring: READ the actual call sites "
            "in agent.py before writing any mock — the LLM request body "
            "lives at kwargs['json']['messages'], and streaming responses "
            "expose iter_lines(); guessing mock shapes without reading the "
            "implementation is the documented failure mode of this issue."),
        "measure": ("python3 -m pytest tests/test_cicd_guards.py -q "
                    "-k 'tool_validation_missing_path or cicd_pr_capture'"),
        "difficulty": "pathological",
    },
}


def sh(cmd, cwd=None, timeout=600):
    p = subprocess.run(["bash", "-c", cmd], cwd=cwd, timeout=timeout,
                       capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def make_clone(issue_id, spec, rep):
    path = os.path.join(REPLAY_ROOT, f"{issue_id.lower()}-r{rep}")
    if os.path.exists(path):
        sh(f"rm -rf {path}", timeout=60)
    os.makedirs(REPLAY_ROOT, exist_ok=True)
    rc, out = sh(f"git clone --shared --quiet --no-checkout {REPO} {path} "
                 f"&& git -C {path} checkout --detach --quiet {spec['base']}",
                 timeout=300)
    if rc != 0:
        raise RuntimeError(f"clone failed for {issue_id}: {out[-400:]}")
    return path


def remove_clone(path):
    if path.startswith(REPLAY_ROOT):
        sh(f"rm -rf {path}", timeout=60)


def run_measure(spec, clone_path):
    """Run the measurement command; for inner-worktree issues, measure from
    a FRESH worktree of the clone's current HEAD (includes agent commits)."""
    if not spec.get("measure_in_inner_worktree"):
        return sh(spec["measure"], cwd=clone_path, timeout=600)
    wt = os.path.join(clone_path, ".measure-wt")
    sh(f"git -C {clone_path} worktree remove --force .measure-wt", timeout=60)
    rc, out = sh(f"git -C {clone_path} worktree add --force .measure-wt HEAD",
                 timeout=120)
    if rc != 0:
        return rc, "inner worktree add failed: " + out[-300:]
    try:
        return sh(spec["measure"], cwd=wt, timeout=600)
    finally:
        sh(f"git -C {clone_path} worktree remove --force .measure-wt",
           timeout=60)


def build_seed(spec):
    if "seed_text" in spec:
        body = spec["seed_text"]
    else:
        with open(os.path.join(REPO, spec["seed_plan"])) as f:
            body = f.read()
        # Strip GitHub verbs the local replay must not perform.
        body = re.sub(r"(?im)^.*\b(gh (pr|issue)|git push|Closes #\d+).*$",
                      "", body)
    return SEED_HEADER + body + (
        "\n\nMEASUREMENT COMMAND (your success criterion):\n"
        f"  {spec['measure']}\n")


def write_run_config(wt_path, turn_cap, backend_config=None, success_check=None):
    """Per-run agent config in the clone (agent.py loads CWD config).

    backend_config: path to an existing agent config.json whose `backends`
    (and `retry`/`context`) blocks are merged in — e.g.
    /droid/repos/test/config.json to run the baseline through the UCSB AWS
    gateway (Claude Sonnet) instead of the local llama server.
    """
    cfg_dir = os.path.join(wt_path, ".agent")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {
        "cycle": {"max_turns": turn_cap},
        "generation": {"temperature": 0.0, "top_p": 1.0, "top_k": 1,
                       "presence_penalty": 0.0},
    }
    if success_check:
        # WS10.c: the agent cannot end_cycle while the issue's own
        # measurement fails (baseline dominant failure mode: M2/P1
        # completion-discipline exits with the measurement still red).
        cfg["cycle"]["success_check"] = success_check
    if backend_config:
        with open(backend_config) as f:
            src = json.load(f)
        for key in ("backends", "retry", "context"):
            if isinstance(src.get(key), dict):
                cfg[key] = src[key]
    cfg_path = os.path.join(cfg_dir, "config.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(cfg_path, 0o600)  # may embed backend API keys


def run_agent(wt_path, seed, turn_cap, agent_args, timeout, backend_config=None,
              success_check=None):
    write_run_config(wt_path, turn_cap, backend_config, success_check)
    cmd = [sys.executable, os.path.join(REPO, "agent.py"),
           "--auto", "--role", "creature", "--no-tui"] + agent_args + [seed]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=wt_path, timeout=timeout,
                           capture_output=True, text=True)
        rc, timed_out = p.returncode, False
        tail = ((p.stdout or "") + (p.stderr or ""))[-2000:]
    except subprocess.TimeoutExpired as e:
        rc, timed_out = -1, True
        tail = ((e.stdout or b"").decode(errors="replace")
                + (e.stderr or b"").decode(errors="replace"))[-2000:]
    return {"agent_rc": rc, "timed_out": timed_out,
            "duration_s": round(time.time() - t0, 1), "tail": tail}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--issues", default=",".join(ISSUES))
    ap.add_argument("--k", type=int, default=1, help="repetitions (pass@k)")
    ap.add_argument("--turn-cap", type=int, default=60)
    ap.add_argument("--agent-timeout", type=int, default=3600)
    ap.add_argument("--agent-arg", action="append", default=[],
                    help="extra args passed through to agent.py (repeatable)")
    ap.add_argument("--backend-config", default=None,
                    help="path to an agent config.json whose backends/retry/"
                         "context blocks are merged into each per-run config "
                         "(e.g. /droid/repos/test/config.json for UCSB Claude)")
    ap.add_argument("--tag", default="",
                    help="run tag appended to clone dirs + results filename "
                         "so concurrent runs (e.g. gemma vs sonnet) don't "
                         "collide")
    ap.add_argument("--no-success-check", action="store_true",
                    help="disable the WS10.c end_cycle success-check gate "
                         "(for A/B against the 2026-07-10 baselines, which "
                         "ran WITHOUT it)")
    ap.add_argument("--keep", action="store_true", help="keep worktrees")
    args = ap.parse_args()

    wanted = [i.strip().upper() for i in args.issues.split(",") if i.strip()]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = ("-" + re.sub(r"[^\w-]", "", args.tag)) if args.tag else ""
    results_path = os.path.join(REPLAY_ROOT, f"results{tag}-{stamp}.jsonl")
    os.makedirs(REPLAY_ROOT, exist_ok=True)
    rows = []

    for issue_id in wanted:
        spec = ISSUES.get(issue_id)
        if spec is None:
            print(f"!! unknown issue {issue_id}, skipping")
            continue
        reps = args.k if args.live else 1
        for rep in range(1, reps + 1):
            wt = make_clone(issue_id + tag, spec, rep)
            row = {"issue": issue_id, "title": spec["title"], "rep": rep,
                   "base": spec["base"], "difficulty": spec["difficulty"],
                   "mode": "live" if args.live else "dry-run"}
            try:
                seed = build_seed(spec)
                row["seed_chars"] = len(seed)
                base_rc, base_out = run_measure(spec, wt)
                row["measure_rc_at_base"] = base_rc
                row["gap_detectable"] = base_rc != 0
                if args.live:
                    sc = None if args.no_success_check else spec["measure"]
                    row.update(run_agent(wt, seed, args.turn_cap,
                                         args.agent_arg, args.agent_timeout,
                                         args.backend_config, sc))
                    fin_rc, fin_out = run_measure(spec, wt)
                    row["measure_rc_final"] = fin_rc
                    row["success"] = fin_rc == 0
                    row["measure_tail"] = fin_out[-500:]
                else:
                    row["measure_tail"] = base_out[-300:]
            except Exception as e:
                row["error"] = str(e)[:400]
            finally:
                if not args.keep:
                    remove_clone(wt)
            rows.append(row)
            with open(results_path, "a") as f:
                f.write(json.dumps(row) + "\n")

    # ------------------------------------------------------------- report
    print(f"\n== replay suite v0 ({'LIVE' if args.live else 'DRY-RUN'}) — "
          f"{results_path}")
    if args.live:
        by_issue = {}
        for r in rows:
            by_issue.setdefault(r["issue"], []).append(r)
        total_pass1 = []
        for iid, rs in by_issue.items():
            wins = sum(1 for r in rs if r.get("success"))
            total_pass1.append(wins / len(rs))
            print(f"  {iid} [{rs[0]['difficulty']:12s}] pass {wins}/{len(rs)}"
                  f"  (turns capped {args.turn_cap})")
        if total_pass1:
            print(f"  overall mean pass@1: "
                  f"{sum(total_pass1) / len(total_pass1):.0%} "
                  f"(tier bars: local-base ≥50%, local-strong ≥70%)")
    else:
        ok = True
        for r in rows:
            flag = "OK " if r.get("gap_detectable") else "BAD"
            if "error" in r:
                flag, ok = "ERR", False
            elif not r.get("gap_detectable"):
                ok = False
            print(f"  [{flag}] {r['issue']} base={r['base']} "
                  f"measure_rc_at_base={r.get('measure_rc_at_base')} "
                  f"({'gap detectable' if r.get('gap_detectable') else 'NO GAP — measurement invalid'})"
                  + (f" error={r['error']}" if "error" in r else ""))
        print("  dry-run verdict:",
              "all measurement signals valid" if ok else "FIX MEASUREMENTS")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
