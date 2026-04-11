# 0006 — root-docstring-cleanup

**Issue**: #11 — friction: top-level .py files still carry the stale SHARED RUNTIME / tool-agent/ banner cycle 0005 missed
**Branch**: cicd/0006-root-docstring-cleanup

## Goal

Finish the job cycle 0005 started: strip the stale `SHARED RUNTIME — DO NOT MODIFY. This file is part of tool-agent/ …` banner from the three top-level modules that still carry it (`agent.py`, `token_utils.py`, `tool_recovery.py`), and broaden the `tests/test_tools_docstrings.py` regression gate to walk the whole repo instead of only `tools/`, so no future top-level file can silently grow the same lie.

## Motivation

Cycle 0005 (#9 → PR #10) explicitly claimed the regression test would stop the stale banner from coming back. It does — but only under `tools/`. The same banner is still sitting in `agent.py:6`, `token_utils.py:4`, and `tool_recovery.py:3`, on files that every CICD cycle routinely modifies. Issue #11 documents the scope gap. Static grep against current `main`:

```bash
grep -rEl 'SHARED RUNTIME|tool-agent/' --include='*.py' . \
  | grep -v '^./tests/test_tools_docstrings.py$' \
  | sort
```

produces:

```
./agent.py
./token_utils.py
./tool_recovery.py
```

The `agent.py` case is the most glaring: `agent.py` is the main script, the first thing any contributor or LLM reads, and its module docstring opens with a "DO NOT MODIFY" warning that cycle 0001 already violated when it retargeted `load_extra_tools`.

There is no runtime impact — module docstrings are strings — so the fix is behaviorally safe. The point is to close cycle 0005's scope gap and pin it with a test that covers the whole repo.

## Success metric

- **Primary**: count of tracked `*.py` files (excluding `tests/test_tools_docstrings.py` itself, since the failure messages in that file legitimately name the forbidden phrases) matching either `SHARED RUNTIME` or `tool-agent/`.
- **Baseline**: 3 (`./agent.py`, `./token_utils.py`, `./tool_recovery.py`). Captured via:
  ```bash
  grep -rEl 'SHARED RUNTIME|tool-agent/' --include='*.py' . \
    | grep -v '^./tests/test_tools_docstrings.py$' \
    | wc -l
  ```
- **Target**: 0.
- **Secondary gate**: `tests/test_tools_docstrings.py` walks the whole repo rooted at the repo root (not just `tools/`) and still passes after the edits. Before the code edits, the broadened test must fail on the three offenders; after, it must pass. That flip is the proof that the new scope actually defends the whole repo.
- **No-regression probe**: a smoke-import test that `python3 -c "import agent, token_utils, tool_recovery"` (from a fresh cwd, with the repo on `sys.path`) still succeeds after the edit. Baseline captured in Phase 1 (`imports OK`). Module docstrings are runtime-inert so this is belt-and-suspenders, but it costs nothing and catches any accidental syntax error I introduce while rewriting the docstrings.

## Scope

- **In**:
  - `agent.py` — replace lines 2–10 (the module docstring block) with an accurate shorter one-liner docstring. Keep the shebang on line 1.
  - `token_utils.py` — replace lines 1–9 (the module docstring block) with an accurate one, keeping the "Gemma 3 tokenizer, falls back to char-based" explanation.
  - `tool_recovery.py` — replace lines 1–10 (the module docstring block) with an accurate one, keeping the "lightweight recovery LLM call on tool errors" explanation.
  - `tests/test_tools_docstrings.py` — broaden the walker from `TOOLS_DIR = .../tools` to the repo root; exclude the test file itself and anything under `__pycache__/`; update the `TestToolsDocstringsAreAccurate` class name to `TestPythonDocstringsAreAccurate` (class is about Python source, not just `tools/`); update failure messages to name the full repo-relative path, not just the filename, so the next cycle can see at a glance where the offender lives.
- **Out**:
  - Any non-`*.py` file (the `plan/` directory legitimately references the historical `tool-agent/` name in cycle documentation).
  - `README.md` / any markdown.
  - Any other cleanup in the touched files beyond the docstring. I am not refactoring `agent.py`'s imports or anything else.
  - Adding further tool/feature changes — this cycle is a cleanup, not a capability bump.

## Implementation steps

1. **Capture baseline** (already done in Phase 1, recorded here for the results file):
   - `grep -rEl 'SHARED RUNTIME|tool-agent/' --include='*.py' .` → 3 offenders (excluding the test file).
   - Smoke import of `agent`, `token_utils`, `tool_recovery` → OK.
   - `python3 -m unittest discover tests` → 110/110 passing.
2. **Broaden the test first, watch it go red.** Edit `tests/test_tools_docstrings.py` to walk the whole repo and run it. It must fail on `agent.py`, `token_utils.py`, `tool_recovery.py` (and stay passing on everything else). That proves the broadened test has real signal.
3. **Edit `agent.py`**: replace the docstring block with an accurate one-liner. Draft:
   ```python
   """Main agent script.

   Connects to llama-server and runs the agentic tool-calling loop.
   Entry points: run_agent_interactive() for interactive use, run_agent() for
   single-prompt runs. See README.md for CLI flags.
   """
   ```
4. **Edit `token_utils.py`**: replace the docstring block with an accurate one-liner. Draft:
   ```python
   """Token utilities with Gemma 3 tokenizer counting.

   Uses the Gemma 3 tokenizer (shared by Gemma 4 variants) for precise context
   window management. Falls back to conservative character-based estimation if
   the tokenizer is unavailable.
   """
   ```
5. **Edit `tool_recovery.py`**: replace the docstring block with an accurate one-liner. Draft:
   ```python
   """Conversational tool recovery.

   When a tool call fails due to missing or invalid parameters, this module
   makes a lightweight LLM call to recover the corrected value and re-executes
   the tool. Only triggers on errors — the happy path is unchanged.
   """
   ```
6. **Re-run the broadened test.** It must now pass.
7. **Re-run the full suite.** Must be 110/110 → 110/110 (no new tests are added by this cycle — the same two tests in `test_tools_docstrings.py` now cover more files).
8. **Smoke-import gate**: `python3 -c "import agent, token_utils, tool_recovery"` from a fresh cwd with the repo on `sys.path`. Must print `imports OK`.
9. **Re-run the metric grep.** Must print 0.
10. **Commit in three reviewable chunks**:
    - `CICD 0006 (#11): broaden tools-docstrings regression test to walk the whole repo` — the test file edit plus class rename. Verified red against current HEAD before step 11.
    - `CICD 0006 (#11): replace stale SHARED RUNTIME docstring in agent.py, token_utils.py, tool_recovery.py` — the three module edits.
    - `CICD 0006 (#11): record cycle 0006 plan, results, and progress log` — the plan file, results file, and `plan/CICD/progress.md` row.

## Test plan

- **Existing tests that must stay green**:
  - All 110 currently passing tests under `tests/`.
  - Specifically `tests/test_tools_docstrings.py::TestToolsDocstringsAreAccurate` (after renaming to `TestPythonDocstringsAreAccurate` and broadening). Its two test methods — `test_no_stale_shared_runtime_banner` and `test_no_stale_tool_agent_reference` — must stay green, but now with whole-repo scope.
- **New tests I'll add**: none. The two existing tests now cover a larger surface. Adding new test methods would be change-for-change's sake.
- **Re-run probe**: smoke-import of the three touched modules before and after. Baseline: `imports OK`. Expected after: `imports OK`. If this flips, I've introduced a syntax error in a docstring rewrite and must fix it before committing.

## Risks & mitigations

- **Risk**: broadening the walker picks up a legitimate use of the phrase elsewhere in the tree (e.g. a future test file that intentionally asserts on the phrase, or a doc fixture).
  **Mitigation**: the walker skips its own path via `Path(__file__).resolve()`, and excludes `__pycache__/`. Scoping to `*.py` only (not `*.md`) keeps `plan/CICD/*.md` legal. Anything else that trips is almost certainly a real stale reference worth catching.
- **Risk**: the broadened test picks up a `.py` file in a directory I didn't think about (e.g. a virtualenv the contributor dropped in the repo root). Walking everything could produce a noisy failure.
  **Mitigation**: add a filter to skip any `.py` under `__pycache__/`, `.git/`, `.venv/`, `venv/`, `env/`, and `node_modules/` — the standard "don't walk virtualenvs" set. The check is a simple substring skip and is documented inline as "virtualenv skiplist".
- **Risk**: I rename the class from `TestToolsDocstringsAreAccurate` to `TestPythonDocstringsAreAccurate` and something else in the test suite (or CI config) references the old name.
  **Mitigation**: before renaming, grep the whole worktree for `TestToolsDocstringsAreAccurate`. If any hit outside the file itself, leave the class name alone and just update the docstring/failure messages.
- **Risk**: the `agent.py` docstring is imported as `agent.__doc__` somewhere (e.g. `--help` output, or an on-disk dump of the doc).
  **Mitigation**: grep for `agent.__doc__` and `__doc__` across the tree. If nothing references it programmatically, this is purely cosmetic and safe. I'll record the grep result in the results file.
- **Risk**: A docstring rewrite accidentally breaks a triple-quote balance in `agent.py` (it's a 2000-line file and tricky to Edit into).
  **Mitigation**: use a targeted Edit with a uniquely-identifiable `old_string` covering the full current docstring block. Smoke-import afterwards will catch any syntax error immediately. If the Edit fails, fall back to Read-then-Write only on the top 15 lines.

## Rollback

If verification cannot reach green after three debug iterations:

```bash
cd /mnt/droid/repos/agent
git worktree remove /tmp/agent-cicd/0006-root-docstring-cleanup --force
git branch -D cicd/0006-root-docstring-cleanup
```

The parent `main` checkout is never touched during this cycle, so there is nothing to revert there. Follow the null-result path in `plan/CICD/agent.md` Phase 8.

## Closes

Closes #11
