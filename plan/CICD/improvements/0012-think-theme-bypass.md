# 0012 — think-theme-bypass

**Issue**: #22 — bug: tools/think.py uses hardcoded ANSI escapes that bypass NO_COLOR / theme.py
**Branch**: cicd/0012-think-theme-bypass

## Goal

Delete the three hardcoded ANSI escape string literals in `tools/think.py` and route its console output through `theme.py`, so the think tool honors `NO_COLOR=1` and pipe/file output the same way every other print site in the codebase already does. Lock the fix in with a repo-walk regression test asserting no file under `tools/` contains a raw `\033[` literal.

## Motivation

Cycle 0012 PROBE: ran a minimal Python snippet under `NO_COLOR=1` that imports both `theme` and `tools.think` and prints their color primitives.

- `theme.c(theme.SKY, "x")` → `'x'` (plain — NO_COLOR honored)
- `theme.dim("x")` → `'x'` (plain — NO_COLOR honored)
- `think.BLUE` → `'\x1b[34m'` (raw escape — NO_COLOR bypassed)
- `think.DIM` → `'\x1b[2m'` (raw escape — NO_COLOR bypassed)
- `think.RESET` → `'\x1b[0m'` (raw escape — NO_COLOR bypassed)

Printing the constants produces visible `\x1b[...]` bytes in the probe log even though `NO_COLOR=1` was set before module import. Probe log: `/tmp/agent-cicd/probes/0012-theme-bypass-before.log`.

`theme.py`'s module docstring explicitly promises: *"All color output honors NO_COLOR and falls back"*. `tools/think.py` is the only file under `tools/` that defines raw escape constants, so it is the only violator (confirmed by `grep -rn '\\033\[' tools/ --include='*.py'` — 3 hits, all in `tools/think.py`).

Prior art for "grep-walk regression on tools/*.py":
- Cycle 0005 (tools-docstring-cleanup): walk assertion that no `tools/*.py` carries a stale `SHARED RUNTIME`/`tool-agent/` docstring.
- Cycle 0006 (root-docstring-cleanup): extended that walk to the whole repo.
- Cycle 0010 (doc-sync-stale-tui): grep-based regression test guarding doc drift.

Same idiom, same deterministic metric shape, same model-independent verification — critically, this cycle does not rely on the noisy P-bug probe that made cycle 0011 null-result.

## Success metric

- **Metric**: `grep -c '\\033\[' tools/think.py` — count of source lines in `tools/think.py` containing the literal Python escape `\033[` (the source form of an ESC byte followed by `[`, used to open any ANSI SGR sequence).
- **Baseline**: **3** (source lines 12, 13, 14 — `BLUE`, `DIM`, `RESET` constant definitions).
- **Target**: **0**.
- **Plus (no-regression)**: full test suite stays green, **119 → 120** passing (119 existing + 1 new regression test).
- **Plus (no-regression)**: `tools/think.py` still imports cleanly and its `definition` dict is unchanged.
- **Measurement method**:

  ```bash
  # Primary metric — must print 0
  grep -c '\\033\[' tools/think.py

  # Repo-walk guard — must print nothing (no file hits)
  grep -rl '\\033\[' tools/ --include='*.py'

  # Suite — must print 'OK' and 120 tests
  python3 -m unittest discover tests 2>&1 | tail -5
  ```

  The `'\\033\['` shell pattern matches the literal 5-character Python source sequence `\033[` (backslash, zero, three, three, bracket). This is exactly how Python source writes an ESC-byte-followed-by-bracket inside a string literal. Python compiles that into the single ESC byte at runtime; the grep targets the source spelling so it runs against the file as it sits on disk without having to import anything.

## Scope

- **In**:
  - `tools/think.py` — remove the `BLUE`/`DIM`/`RESET` constants at lines 12–14, replace the three print sites and the spinner start prefix with `theme.c(theme.SKY, …)` / `theme.dim(…)` calls, and add `import theme` at the top.
  - `tests/test_tools_no_raw_ansi.py` — new file, one test class `TestToolsNoRawAnsi`, one test method asserting no file under `tools/` contains the literal substring `\033[` in its source bytes. Failure message names the offending file(s) and line numbers.
- **Out**:
  - No change to the `fn()` signature, return value, request body, or logging calls. This is a presentation-layer fix only.
  - No change to the `definition` dict (tool description, parameters, required fields all stay identical).
  - No change to any other tool under `tools/`. The repo-walk passes today for every other file — I'm only fixing the one violator.
  - No change to `theme.py` or `spinner.py`. The spinner's `StreamStatus.start(prefix)` already accepts any string; passing a pre-themed prefix (which is plain text under `NO_COLOR=1`) needs no API change.
  - No behavioral change under a color-capable terminal with `NO_COLOR` unset. The reasoning block should still render dim, and the `[Thinking]` spinner prefix should still render in a blue-ish Aurora shade. Color choice is allowed to shift slightly (Aurora Sky instead of raw 8-color blue) — the constraint is "themed, not raw", not "pixel-identical shade".

## Implementation steps

1. **Read `tools/think.py` end-to-end** and confirm the only escape usages are the module-level constants at lines 12–14 and their references at lines 86 (`status.start(...)`), 120 (`print(f"  {DIM}[Reasoning]{RESET}")`), 121 (`print(f"  {DIM}{reasoning}{RESET}")`). No other escape sequences hide in the file.
2. **Delete lines 12–14** (the `BLUE`, `DIM`, `RESET` module-level constants).
3. **Add `import theme`** to the top-of-file import block, in the group that matches the existing ordering.
4. **Rewrite line 86** (spinner start). Was:
   ```python
   status.start(f"  {BLUE}[Thinking] ")
   ```
   Becomes:
   ```python
   status.start("  " + theme.c(theme.SKY, "[Thinking] "))
   ```
   `theme.SKY` is the Aurora palette's blue stop — the closest honest replacement for the previous raw `\033[34m`. Under `NO_COLOR=1` / pipe, `theme.c` returns plain text and the spinner line stays readable.
5. **Rewrite lines 120–121** (reasoning block). Was:
   ```python
   print(f"  {DIM}[Reasoning]{RESET}")
   print(f"  {DIM}{reasoning}{RESET}")
   ```
   Becomes:
   ```python
   print("  " + theme.dim("[Reasoning]"))
   print("  " + theme.dim(reasoning))
   ```
   `theme.dim` already handles the `NO_COLOR` fallback and emits the correct SGR sequence otherwise.
6. **Re-read the file** and confirm no `\033[` literals remain. `grep -c '\\033\[' tools/think.py` must print `0`.
7. **Add `tests/test_tools_no_raw_ansi.py`** — one `unittest.TestCase` class with one test method. Walks `os.listdir("tools")` for `*.py` files (skipping `__init__.py` and `__pycache__`), reads each file as text, asserts the literal substring `'\\033['` is absent. On failure, names each offending file and the first offending line number so a future regression is instantly diagnosable.
8. **Run the full test suite** (`python3 -m unittest discover tests`). Expect `Ran 120 tests`, `OK`.
9. **Smoke check**: `python3 -c "from tools import think; print(hasattr(think, 'BLUE'))"` — expect `False`. And a quick import-under-`NO_COLOR=1` sanity run to confirm `tools.think` still imports cleanly.
10. **Commit in three logical chunks inside the worktree**:
    - (a) `tools/think.py` — route through theme.
    - (b) `tests/test_tools_no_raw_ansi.py` — regression guard.
    - (c) `plan/CICD/improvements/0012-*.md` + `0012-*.results.md` + `plan/CICD/progress.md` row.

    The final commit on the branch body contains `Closes #22` so the PR picks it up automatically.

## Test plan

- **Existing tests that must stay green** (119 total):
  - `tests/test_callbacks.py`, `tests/test_commands.py`
  - `tests/test_file_tool.py` (cycle 0004 auto-mkdir description assertion — untouched)
  - `tests/test_search_files.py` (cycles 0007, 0009)
  - `tests/test_tools_docstrings.py` (cycles 0005, 0006 — docstring walk over `tools/*.py` from a different angle; new file must not regress the docstring walk)
  - `tests/test_agent_console_dedup.py` (cycle 0008)
  - `tests/test_doc_sync.py` (cycle 0010)
  - `tests/test_cancel.py`, `tests/test_spinner.py`, `tests/test_tui.py` (ui-upgrade-followup)
  - plus the rest.
- **New tests I'll add**:
  - `tests/test_tools_no_raw_ansi.py::TestToolsNoRawAnsi::test_no_raw_ansi_escapes_in_tools` — walks `tools/*.py`, asserts the literal source substring `'\033['` is absent in every file. Names offenders on failure. Complements the `test_tools_docstrings` walk without touching it.
- **Re-run probe**: same Python snippet from Phase 2, after the edit, captured to `/tmp/agent-cicd/probes/0012-theme-bypass-after.log`. Expected: `hasattr(think, 'BLUE')` is `False`, `theme.dim("[Reasoning]")` returns plain `[Reasoning]` under `NO_COLOR=1`, and the import still succeeds.

## Risks & mitigations

- **Risk**: removing the constants breaks an external importer of `tools.think.BLUE`.
  - **Mitigation**: `grep -rn 'think\.BLUE\|think\.DIM\|think\.RESET' .` before committing. Expected: zero hits (these constants were module-private in intent).
- **Risk**: `theme.SKY` renders in a different shade than the old raw `\033[34m`, so colored terminals see a slightly different color.
  - **Mitigation**: Acceptable — the requirement is "themed, not raw". The Aurora palette is the codebase's single source of truth for colors, and this just brings `think.py` in line. No existing test asserts on the specific shade.
- **Risk**: the new regression test is too strict and blocks a legitimate future escape usage (e.g., someone adds a cursor-control sequence on purpose).
  - **Mitigation**: The test only walks `tools/*.py`. Cursor control, if ever needed, belongs in `theme.py` or a new `ansi_utils.py`, not inside a tool. A future cycle that needs a targeted exception can edit the test to whitelist a specific pattern — but the default posture "tools should not raw-print escapes" is correct.
- **Risk**: `theme.py` import creates a circular dependency via some chain I haven't noticed.
  - **Mitigation**: `theme.py` has no imports from `tools/` (verified by grep). The import is one-directional and safe.

## Rollback

Single-branch cycle. If verification fails after three debug iterations, abort cleanly:

```bash
cd /mnt/droid/repos/agent
git worktree remove /tmp/agent-cicd/0012-think-theme-bypass --force
git branch -D cicd/0012-think-theme-bypass
```

The parent checkout at `/mnt/droid/repos/agent` was never touched. No rollback needed on `main`.

If the branch is merged and a revert is needed later, the revert is a single `git revert` of the merge commit — the three edits (tool, test, docs) are all on one branch.

## Closes

Closes #22
