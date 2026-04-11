# 0012 — think-theme-bypass — results

- Issue: #22
- Branch: cicd/0012-think-theme-bypass
- PR: (pending)
- Commit range: 50cedd2..HEAD
- Date: 2026-04-11

## Metric

- **Primary**: `grep -c '\\033\[' tools/think.py`
  - Baseline: **3** (lines 12, 13, 14 — `BLUE`, `DIM`, `RESET` constants)
  - After:    **0**
  - Delta:    **−3 (−100%)**
- **Repo-walk guard**: `grep -rl '\\033\[' tools/ --include='*.py'`
  - Baseline: 1 file (`tools/think.py`)
  - After:    0 files
  - Delta:    −1 (−100%)

## Test suite

- Before: **119** passing
- After:  **120** passing (119 existing + 1 new regression test)

## Probe re-run

- Before log: `/tmp/agent-cicd/probes/0012-theme-bypass-before.log`
- After log:  `/tmp/agent-cicd/probes/0012-theme-bypass-after.log`
- Verdict:    **PASS**
  - Under `NO_COLOR=1`:
    - `theme.c(theme.SKY, "x")` → `'x'` (unchanged)
    - `theme.dim("x")` → `'x'` (unchanged)
    - `hasattr(think, "BLUE")` → **False** (was `True`, returning `'\x1b[34m'`)
    - `hasattr(think, "DIM")` → **False** (was `True`, returning `'\x1b[2m'`)
    - `hasattr(think, "RESET")` → **False** (was `True`, returning `'\x1b[0m'`)
  - Simulated `print("  " + theme.dim("[Reasoning]"))` under `NO_COLOR=1` outputs `  [Reasoning]` with zero escape bytes.
  - `tools.think` module still imports cleanly; `definition` dict is byte-for-byte identical to the baseline (not touched).

## What I actually changed

- `tools/think.py` (net −3 lines)
  - Deleted module-level `BLUE = "\033[34m"`, `DIM = "\033[2m"`, `RESET = "\033[0m"` constants.
  - Added `import theme` to the top-of-file import block.
  - `status.start(f"  {BLUE}[Thinking] ")` → `status.start("  " + theme.c(theme.SKY, "[Thinking] "))`.
  - `print(f"  {DIM}[Reasoning]{RESET}")` → `print("  " + theme.dim("[Reasoning]"))`.
  - `print(f"  {DIM}{reasoning}{RESET}")` → `print("  " + theme.dim(reasoning))`.
  - `fn()` signature, return value, request body, logging calls, and the `definition` dict are all unchanged.
- `tests/test_tools_no_raw_ansi.py` (new file, 46 lines)
  - One `TestToolsNoRawAnsi` class, one `test_no_raw_ansi_escapes_in_tools` method.
  - Walks `tools/*.py` (skipping `__init__.py`), reads each file as text, asserts the literal source substring `'\033['` is absent.
  - On failure, names each offending file and the first offending line number.
  - Verified the negative case by monkey-patching `Path.read_text` to inject a fake offender — the test failed with `['tools/think.py:1']` in the failure message, confirming the guard actually catches the regression it's designed to catch.

## What I learned

- **Deterministic grep-metric cycles are immune to the model-variance trap** that made cycle 0011 null-result. The whole 0012 cycle from issue-file to green verify took under ten minutes and never ran a live LLM probe. When the target is a defect that can be measured by reading the repo, don't pay the probe tax.
- **`theme.py` honors `NO_COLOR` but is not the only place escape bytes can originate.** A repo audit for `\\033\[` surfaced exactly one violator (`tools/think.py`) — worth re-running this walk periodically because it catches the kind of regression that would otherwise only be noticed by a user piping agent output into `less`.
- **Module-level "private" constants can and do become a public API surface over time.** The three constants were clearly intended as private helpers, but a downstream caller could have imported them. A grep outside the file confirmed zero external references before deletion — a 30-second check that would have saved real pain had the check come back non-empty.
- **The `tools/` directory is a good place for invariants.** Cycles 0005/0006/0010/0012 have all added grep-based repo-walks as regression guards. The pattern is cheap, fast, and catches an entire class of drift (stale docstrings, stale doc refs, raw escapes) that behavioral tests miss. A future "enforce invariants" file listing the set of things `tools/` must not contain could consolidate these, but that's scope expansion beyond this cycle.
