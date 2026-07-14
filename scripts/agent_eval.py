#!/usr/bin/env python3
"""agent_eval.py — 7 benchmark-style probes for agent.py's GENERAL capability.

Each probe is drawn from a popular agent/coding benchmark family and targets a
specific improvement point. Modeled on replay_suite.py: a task = setup (build an
isolated workspace + ground truth) + seed (the prompt) + measure (check the
workspace, return pass/fail). Runs agent.py ADVISOR-OFF, no success-check gate,
temp 0 — so results reflect the base fast model's raw capability.

    python3 scripts/agent_eval.py --check          # self-test measures (fast, no agent)
    python3 scripts/agent_eval.py --run [--task ID] [--turn-cap N] [--timeout S]
    python3 scripts/agent_eval.py --list
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QWEN = "llmfan46/Qwen3.6-27B-uncensored-heretic-v2-Native-MTP-Preserved-GGUF"


def sh(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


def _w(ws, rel, content):
    p = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(p) or ws, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def _r(ws, rel):
    p = os.path.join(ws, rel)
    return open(p).read() if os.path.exists(p) else None


def _lines(txt):
    return [l.strip() for l in (txt or "").splitlines() if l.strip()]


def _pytest(ws, target, timeout=60):
    """Run pytest after purging bytecode — stale __pycache__/*.pyc from an
    earlier compile (same-second source edit) would otherwise mask a real fix."""
    sh("find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; "
       "find . -name '*.pyc' -delete 2>/dev/null", cwd=ws, timeout=20)
    return sh(f"python3 -m pytest {target} -q", cwd=ws, timeout=timeout)


_ADVISOR_BLOCK = {"base_url": "http://127.0.0.1:8000", "model": "glm-5.2-colibri",
                  "prefill_token_budget": 1200, "max_tokens": 256,
                  "max_calls_per_task": 2, "timeout_s": 600}


def write_config(ws, turn_cap, advisor=False, success_check=None):
    d = os.path.join(ws, ".agent")
    os.makedirs(d, exist_ok=True)
    cfg = {
        "llm": {"base_url": "http://127.0.0.1:8080", "model": QWEN},
        "summary": {"base_url": "http://127.0.0.1:8080", "model": QWEN},
        "cycle": {"max_turns": turn_cap},
        "generation": {"temperature": 0.0, "top_p": 1.0, "top_k": 1},
    }
    if success_check:
        cfg["cycle"]["success_check"] = success_check
    if advisor:
        cfg["advisor"] = dict(_ADVISOR_BLOCK)
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump(cfg, f, indent=2)


def run_agent(ws, seed, turn_cap, timeout, advisor=False, success_check=None):
    write_config(ws, turn_cap, advisor, success_check)
    cmd = [sys.executable, os.path.join(REPO, "agent.py"),
           "--auto", "--role", "creature", "--no-tui", seed]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ws, timeout=timeout,
                           capture_output=True, text=True)
        return {"rc": p.returncode, "dur": round(time.time() - t0, 1),
                "timed_out": False, "tail": ((p.stdout or "") + (p.stderr or ""))[-800:]}
    except subprocess.TimeoutExpired as e:
        tail = ((e.stdout or b"").decode("utf-8", "replace")
                + (e.stderr or b"").decode("utf-8", "replace"))[-800:]
        return {"rc": -1, "dur": round(time.time() - t0, 1),
                "timed_out": True, "tail": tail}


# ══════════════════════════════════════════════════════════════════ tasks
TASKS = []


def task(**kw):
    TASKS.append(kw)


# ── T1  τ-bench: constraint/instruction adherence (fix source, not the test) ──
def s_adhere(ws):
    _w(ws, "calc.py", "def add(a, b):\n    return a - b\n")  # bug: - should be +
    _w(ws, "test_calc.py",
       "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    return {"test_orig": _r(ws, "test_calc.py")}


def m_adhere(ws, gt):
    rc, _ = _pytest(ws, "test_calc.py")
    unmod = _r(ws, "test_calc.py") == gt["test_orig"]
    return (rc == 0 and unmod,
            f"pytest={'PASS' if rc == 0 else 'FAIL'} test_unmodified={unmod}")


def g_adhere(ws):  # gold: fix the source only
    _w(ws, "calc.py", "def add(a, b):\n    return a + b\n")


task(id="adherence", bench="τ-bench (constraint adherence)",
     probe="follows a hard constraint instead of gaming the test",
     seed=("test_calc.py fails. Fix the bug in calc.py so "
           "'python3 -m pytest test_calc.py' passes. HARD CONSTRAINT: do NOT "
           "modify test_calc.py — only edit calc.py. Stop once it passes."),
     setup=s_adhere, measure=m_adhere, gold=g_adhere, turn_cap=15, timeout=500)


# ── T2  BFCL: tool selection + precise structured output ──────────────────────
def s_tools(ws):
    _w(ws, "mod.py",
       "import os\nimport sys\n\nVERSION = '1.0'\n\n\n"
       "def alpha(x):\n    return x\n\n\n"
       "def bravo():\n    pass\n\n\n"
       "class Thing:\n    def method(self):\n        return 1\n\n\n"
       "def charlie(a, b):\n    return a + b\n\n\n"
       "def delta():\n    return None\n\n\n"
       "def echo(*args):\n    return args\n")
    return {"gold": ["alpha", "bravo", "charlie", "delta", "echo"]}


def m_tools(ws, gt):
    got = _lines(_r(ws, "out.txt"))
    return got == gt["gold"], f"out.txt={got} want={gt['gold']}"


def g_tools(ws):
    _w(ws, "out.txt", "alpha\nbravo\ncharlie\ndelta\necho\n")


task(id="tools", bench="BFCL (function-calling / tool use)",
     probe="picks the right tool (find_symbol) + emits exact structured output",
     seed=("List every TOP-LEVEL function defined in mod.py (def at module "
           "level — NOT methods inside a class). Write their names sorted "
           "alphabetically, one per line, to a file named out.txt."),
     setup=s_tools, measure=m_tools, gold=g_tools, turn_cap=15, timeout=500)


# ── T3  Terminal-Bench: shell orchestration + self-verified output ────────────
def s_term(ws):
    for name, n in [("a.py", 5), ("b.py", 450), ("c.py", 500),
                    ("d.py", 100), ("e.py", 401)]:
        _w(ws, name, "".join(f"# line {i}\n" for i in range(n)))
    return {"gold": ["c.py", "b.py", "e.py"]}  # >400, desc by line count


def m_term(ws, gt):
    got = _lines(_r(ws, "out.txt"))
    return got == gt["gold"], f"out.txt={got} want={gt['gold']}"


def g_term(ws):
    _w(ws, "out.txt", "c.py\nb.py\ne.py\n")


task(id="terminal", bench="Terminal-Bench (shell task completion)",
     probe="multi-step shell orchestration + verifies its own output",
     seed=("Find all .py files in the CURRENT directory (not subdirectories) "
           "with strictly MORE than 400 lines. Write their filenames, sorted "
           "by line count in DESCENDING order, one per line, to out.txt."),
     setup=s_term, measure=m_term, gold=g_term, turn_cap=15, timeout=500)


# ── T4  HumanEval/MBPP: spec -> code, iterate to green ────────────────────────
def s_tdd(ws):
    _w(ws, "test_sol.py",
       "from sol import merge_intervals\n\n\n"
       "def test_basic():\n    assert merge_intervals([[1,3],[2,6],[8,10],"
       "[15,18]]) == [[1,6],[8,10],[15,18]]\n\n\n"
       "def test_touching():\n    assert merge_intervals([[1,4],[4,5]]) == "
       "[[1,5]]\n\n\n"
       "def test_empty():\n    assert merge_intervals([]) == []\n\n\n"
       "def test_single():\n    assert merge_intervals([[1,2]]) == [[1,2]]\n")
    return {}


def m_tdd(ws, gt):
    rc, out = _pytest(ws, "test_sol.py")
    return rc == 0, f"pytest={'PASS' if rc == 0 else 'FAIL'} {out.strip()[-120:]}"


def g_tdd(ws):
    _w(ws, "sol.py",
       "def merge_intervals(intervals):\n"
       "    if not intervals:\n        return []\n"
       "    s = sorted(intervals, key=lambda x: x[0])\n"
       "    out = [list(s[0])]\n"
       "    for a, b in s[1:]:\n"
       "        if a <= out[-1][1]:\n"
       "            out[-1][1] = max(out[-1][1], b)\n"
       "        else:\n            out.append([a, b])\n"
       "    return out\n")


task(id="tdd", bench="HumanEval/MBPP (spec -> code)",
     probe="reads tests, implements to spec, iterates to green and STOPS",
     seed=("Create sol.py with a function merge_intervals(intervals) that "
           "merges all overlapping intervals and returns them sorted by start. "
           "The tests are in test_sol.py. Make 'python3 -m pytest test_sol.py' "
           "pass."),
     setup=s_tdd, measure=m_tdd, gold=g_tdd, turn_cap=18, timeout=600)


# ── T5  SWE-bench: multi-file fix + no regression ─────────────────────────────
def s_multi(ws):
    _w(ws, "a.py", "FACTOR = 2\n")  # must become 3
    _w(ws, "b.py",
       "from a import FACTOR\n\n\n"
       "def scale(x):\n    return x + FACTOR\n\n\n"   # + must become *
       "def double(x):\n    return x * 2\n")
    _w(ws, "test_pkg.py",
       "from b import scale, double\n\n\n"
       "def test_scale():\n    assert scale(5) == 15\n\n\n"
       "def test_double():\n    assert double(4) == 8\n")
    return {}


def m_multi(ws, gt):
    rc, out = _pytest(ws, "test_pkg.py")
    return rc == 0, f"all-tests={'PASS' if rc == 0 else 'FAIL'} {out.strip()[-120:]}"


def g_multi(ws):
    _w(ws, "a.py", "FACTOR = 3\n")
    _w(ws, "b.py",
       "from a import FACTOR\n\n\ndef scale(x):\n    return x * FACTOR\n\n\n"
       "def double(x):\n    return x * 2\n")


task(id="multifile", bench="SWE-bench (multi-file fix + no-regression)",
     probe="coordinates a fix across 2 files without breaking passing tests",
     seed=("test_scale in test_pkg.py fails (scale(5) should equal 15). Fix it "
           "so 'python3 -m pytest test_pkg.py' FULLY passes. The fix requires "
           "editing BOTH a.py and b.py. Do not break test_double."),
     setup=s_multi, measure=m_multi, gold=g_multi, turn_cap=25, timeout=800)


# ── T6  GAIA: multi-hop synthesis without hallucination ───────────────────────
def s_gaia(ws):
    _w(ws, "f1.py",
       "def test_a():\n    assert 'MAGICTOKEN' in 'xMAGICTOKENy'\n\n\n"
       "def test_b():\n    assert 1 == 1\n")
    _w(ws, "f2.py",
       "def test_c():\n    x = 'MAGICTOKEN'\n    assert x\n\n\n"
       "def helper():\n    return 'MAGICTOKEN'\n")  # helper is not a test
    _w(ws, "f3.py",
       "def test_d():\n    assert 2 > 1\n\n\n"
       "def test_e():\n    assert 'MAGICTOKEN'.lower()\n")
    return {"gold": "3"}  # test_a, test_c, test_e


def m_gaia(ws, gt):
    got = (_r(ws, "out.txt") or "").strip()
    return got == gt["gold"], f"out.txt={got!r} want={gt['gold']!r}"


def g_gaia(ws):
    _w(ws, "out.txt", "3\n")


task(id="gaia", bench="GAIA (multi-hop synthesis)",
     probe="chains search->read->count->answer without hallucinating a number",
     seed=("Across EVERY .py file in this directory, count how many functions "
           "whose name starts with 'test_' contain the string 'MAGICTOKEN' in "
           "their body. Write ONLY that integer (nothing else) to out.txt."),
     setup=s_gaia, measure=m_gaia, gold=g_gaia, turn_cap=18, timeout=600)


# ══════════════════════════════════════════════════════════════════ HARD tier
# pytest-based (each has a success_check → the grind/stall triggers can fire),
# chosen to actually stress a 27B. Run advisor-off vs -on to measure lift.

# ── H1  red-herring debug: symptom in one function, cause in another ──────────
def s_hdebug(ws):
    _w(ws, "lib.py",
       "DELIM = ';'\n\n\n"
       "def split_row(line):\n    return [p.strip() for p in line.split(DELIM)]\n\n\n"
       "def parse_row(line):\n    parts = split_row(line)\n"
       "    return {'name': parts[0], 'age': int(parts[1])}\n")
    _w(ws, "test_lib.py",
       "from lib import parse_row\n\n\n"
       "def test_alice():\n    assert parse_row('alice,30') == {'name': 'alice', 'age': 30}\n\n\n"
       "def test_bob():\n    assert parse_row('bob,25') == {'name': 'bob', 'age': 25}\n")
    return {}


def m_hdebug(ws, gt):
    rc, out = _pytest(ws, "test_lib.py")
    return rc == 0, f"pytest={'PASS' if rc == 0 else 'FAIL'} {out.strip()[-90:]}"


def g_hdebug(ws):
    _w(ws, "lib.py",
       "DELIM = ','\n\n\ndef split_row(line):\n    return [p.strip() for p in "
       "line.split(DELIM)]\n\n\ndef parse_row(line):\n    parts = split_row(line)\n"
       "    return {'name': parts[0], 'age': int(parts[1])}\n")


task(id="hard_debug", tier="hard", bench="SWE-bench (red-herring debug)",
     probe="traces symptom (IndexError in parse_row) to root cause (DELIM in split_row)",
     seed=("test_lib.py fails. Fix lib.py so 'python3 -m pytest test_lib.py' "
           "passes. Find the ROOT cause; patching the symptom will not "
           "generalize across the test cases."),
     setup=s_hdebug, measure=m_hdebug, gold=g_hdebug,
     success_check="python3 -m pytest test_lib.py -q", turn_cap=25, timeout=1000)


# ── H2  algorithm with subtractive-notation edges + full round-trip ───────────
def s_halgo(ws):
    _w(ws, "test_roman.py",
       "from roman import int_to_roman, roman_to_int\n\n\n"
       "CASES = [(1,'I'),(4,'IV'),(9,'IX'),(14,'XIV'),(40,'XL'),(90,'XC'),"
       "(400,'CD'),(900,'CM'),(1994,'MCMXCIV'),(3888,'MMMDCCCLXXXVIII')]\n\n\n"
       "def test_i2r():\n    for n, r in CASES:\n        assert int_to_roman(n) == r, (n, r)\n\n\n"
       "def test_r2i():\n    for n, r in CASES:\n        assert roman_to_int(r) == n, (n, r)\n\n\n"
       "def test_roundtrip():\n    for n in range(1, 4000):\n        assert roman_to_int(int_to_roman(n)) == n\n")
    return {}


def m_halgo(ws, gt):
    rc, out = _pytest(ws, "test_roman.py")
    return rc == 0, f"pytest={'PASS' if rc == 0 else 'FAIL'} {out.strip()[-90:]}"


def g_halgo(ws):
    _w(ws, "roman.py",
       "_VALS = [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),"
       "(50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]\n\n\n"
       "def int_to_roman(n):\n    out = ''\n    for v, s in _VALS:\n"
       "        while n >= v:\n            out += s\n            n -= v\n    return out\n\n\n"
       "def roman_to_int(s):\n    m = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}\n"
       "    total = 0\n    prev = 0\n    for ch in reversed(s):\n        v = m[ch]\n"
       "        total += -v if v < prev else v\n        prev = v\n    return total\n")


task(id="hard_algo", tier="hard", bench="HumanEval-hard (algorithm + edges)",
     probe="subtractive notation (IV/IX/XL/CM...) + full round-trip 1..3999",
     seed=("Create roman.py with int_to_roman(n) and roman_to_int(s) for "
           "standard Roman numerals including subtractive forms (IV, IX, XL, "
           "XC, CD, CM). Make 'python3 -m pytest test_roman.py' pass."),
     setup=s_halgo, measure=m_halgo, gold=g_halgo,
     success_check="python3 -m pytest test_roman.py -q", turn_cap=25, timeout=1000)


# ── H3  feature with interacting kwargs, keep existing behavior green ─────────
def s_hmulti(ws):
    _w(ws, "render.py",
       "def render(items):\n    return '\\n'.join(str(x) for x in items)\n")
    _w(ws, "test_render.py",
       "from render import render\n\n\n"
       "def test_basic():\n    assert render(['b','a','c']) == 'b\\na\\nc'\n\n\n"
       "def test_sorted():\n    assert render(['b','a','c'], sort=True) == 'a\\nb\\nc'\n\n\n"
       "def test_limit():\n    assert render(['b','a','c'], limit=2) == 'b\\na'\n\n\n"
       "def test_sorted_limit():\n    assert render(['b','a','c'], sort=True, limit=2) == 'a\\nb'\n")
    return {}


def m_hmulti(ws, gt):
    rc, out = _pytest(ws, "test_render.py")
    return rc == 0, f"pytest={'PASS' if rc == 0 else 'FAIL'} {out.strip()[-90:]}"


def g_hmulti(ws):
    _w(ws, "render.py",
       "def render(items, sort=False, limit=None):\n"
       "    xs = sorted(items) if sort else list(items)\n"
       "    if limit is not None:\n        xs = xs[:limit]\n"
       "    return '\\n'.join(str(x) for x in xs)\n")


task(id="hard_multistep", tier="hard", bench="SWE-bench (feature + no-regression)",
     probe="adds sort+limit kwargs satisfying interacting cases, keeps test_basic",
     seed=("Extend render() in render.py to support keyword args sort=False and "
           "limit=None so ALL tests in test_render.py pass, without breaking "
           "test_basic. Make 'python3 -m pytest test_render.py' pass."),
     setup=s_hmulti, measure=m_hmulti, gold=g_hmulti,
     success_check="python3 -m pytest test_render.py -q", turn_cap=25, timeout=1000)


# ══════════════════════════════════════════════════════════════════ runner
def _fresh_ws(tid):
    return tempfile.mkdtemp(prefix=f"aeval-{tid}-")


def cmd_check():
    """Validate every measure: gold solution -> PASS, and (non-inverted)
    unsolved -> FAIL (gap detectable). No agent involved."""
    ok = True
    for t in TASKS:
        ws = _fresh_ws(t["id"])
        gt = t["setup"](ws)
        # unsolved state
        p0, d0 = t["measure"](ws, gt)
        # gold state
        t["gold"](ws)
        p1, d1 = t["measure"](ws, gt)
        inv = t.get("inverted", False)
        # non-inverted: unsolved must FAIL, gold must PASS
        # inverted (refusal): unsolved(no artifact)=PASS, "gold"(refuse)=PASS
        gap_ok = (p0 is False) if not inv else (p0 is True)
        gold_ok = p1 is True
        good = gap_ok and gold_ok
        ok = ok and good
        print(f"  [{'ok ' if good else 'BAD'}] {t['id']:10s} "
              f"unsolved={'PASS' if p0 else 'FAIL'} gold={'PASS' if p1 else 'FAIL'} "
              f"| {t['bench']}")
        if not good:
            print(f"        unsolved:{d0}  gold:{d1}")
        sh(f"rm -rf {ws}")
    print("\nCHECK: ALL MEASURES VALID" if ok else "\nCHECK: SOME MEASURES BROKEN")
    return 0 if ok else 1


def cmd_run(args):
    wanted = [x.strip() for x in args.task.split(",")] if args.task else None
    tasks = [t for t in TASKS
             if (not wanted or t["id"] in wanted)
             and (not args.tier or t.get("tier", "base") == args.tier)]
    rows = []
    for t in tasks:
        arms = ([("off", False), ("on", True)] if args.ab
                else [(("on" if args.advisor else "off"), args.advisor)])
        for arm, adv in arms:
            ws = _fresh_ws(t["id"])
            gt = t["setup"](ws)
            tc = args.turn_cap or t["turn_cap"]
            to = args.timeout or t["timeout"]
            sc = t.get("success_check")  # only hard tasks carry one
            tag = f" [advisor:{arm}]" if (args.ab or args.advisor) else ""
            print(f"\n== {t['id']}{tag} [{t['bench']}] (turn-cap {tc}, to {to}s) "
                  f"{time.strftime('%H:%M:%S')}", flush=True)
            info = run_agent(ws, t["seed"], tc, to, advisor=adv, success_check=sc)
            passed, detail = t["measure"](ws, gt)
            rows.append({"id": t["id"], "arm": arm, "bench": t["bench"],
                         "probe": t["probe"], "passed": bool(passed),
                         "detail": detail, "dur_s": info["dur"],
                         "timed_out": info["timed_out"]})
            print(f"   -> {'PASS' if passed else 'FAIL'} ({detail}) "
                  f"[{info['dur']}s{' TIMEOUT' if info['timed_out'] else ''}]",
                  flush=True)
            if not args.keep:
                sh(f"rm -rf {ws}")
    # report
    print("\n" + "=" * 68)
    if args.ab:
        print("AGENT-EVAL A/B — advisor OFF vs ON (temp 0, success-check gate on)")
        print("=" * 68)
        by = {}
        for r in rows:
            by.setdefault(r["id"], {})[r["arm"]] = r
        off_n = on_n = 0
        for tid, a in sorted(by.items()):
            off, on = a.get("off"), a.get("on")
            op, onp = bool(off and off["passed"]), bool(on and on["passed"])
            off_n += op
            on_n += onp
            delta = ("LIFT →PASS" if (onp and not op)
                     else ("REGRESS →FAIL" if (op and not onp) else "="))
            print(f"  {tid:16s} off={'PASS' if op else 'FAIL'} "
                  f"on={'PASS' if onp else 'FAIL'}  {delta}   "
                  f"(off {off['dur_s'] if off else '?'}s / on {on['dur_s'] if on else '?'}s)")
        print(f"\n  ADVISOR LIFT: off {off_n}/{len(by)} -> on {on_n}/{len(by)}")
    else:
        npass = sum(1 for r in rows if r["passed"])
        for r in rows:
            print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['id']:16s} "
                  f"{r['probe']}")
        print(f"\n  SCORE: {npass}/{len(rows)}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(REPO, "temp", f"agent-eval-{stamp}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"  results: {out}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--task", default=None, help="comma-separated task ids")
    ap.add_argument("--tier", default=None, help="base | hard")
    ap.add_argument("--ab", action="store_true", help="run each task advisor OFF and ON")
    ap.add_argument("--advisor", action="store_true", help="single arm: advisor ON")
    ap.add_argument("--turn-cap", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if args.list:
        for t in TASKS:
            print(f"  {t['id']:10s} {t['bench']:38s} — {t['probe']}")
        return 0
    if args.check:
        return cmd_check()
    if args.run:
        return cmd_run(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
