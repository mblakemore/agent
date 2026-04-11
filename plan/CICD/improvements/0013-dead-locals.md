# 0013 — dead-locals

**Issue**: #24 — bug: dead local assignments across tools/file.py, agent.py, tool_recovery.py — leftover from prior rewrites
**Branch**: cicd/0013-dead-locals

## Goal

Delete five dead local assignments (assigned-then-never-read) across `tools/file.py`, `agent.py`, and `tool_recovery.py`, and install a regression guard that fails if any of the same functions grow a new dead local later.

## Motivation

Static AST scan in cycle 0013 PERCEIVE turned up five `Store`-only locals spread across three files — cruft from prior rewrites where the line consuming the value was removed but the assignment was left behind.

- `tools/file.py:22` — `resolved = (cwd / p).resolve()` inside `_resolve_path`. Computed, then the function returns via a different branch. Strong smell of an incomplete rewrite: the author probably intended to return `resolved` and abandoned the change.
- `tools/file.py:24` — `cwd_str = str(cwd) + "/"` same function, same pattern.
- `agent.py:1420` — `history_snapshot = len(conversation_history)` at the top of `run_agent_single`. Never read.
- `agent.py:720` — `summary_cfg = self._config.get("summary", {})` inside `_AsyncSummarizer.kick._worker`. Never read; `_summary_request` is called directly without consulting this dict.
- `tool_recovery.py:123` — `base_url = config.get("llm", {}).get("base_url", ...)` inside `_ask_for_param`. Never passed to `llm_call_fn` (which uses the module-level `BASE_URL` through `_llm_request` anyway).

Issue #24 has the full per-file reasoning. Probe log for the cycle's P-bug run that triggered the scan: `/tmp/agent-cicd/probes/0013-pbug-before.log`.

## Success metric

- **Baseline**: dead-local scan across the 3 target files → **5** hits.
- **Target**: **0** hits.
- **Measurement method**: new `tests/test_no_dead_locals.py`. It parses each file with `ast`, walks into the named function(s), collects `Name(Store)` minus `Name(Load)` after stripping the function's own args/self, and asserts the remaining set is empty. Same test asserts 0 hits in all listed files; baseline number is recorded in the test's docstring.
- **Plus**: full test suite stays green (currently 120 passing → 121 after the new test).

## Scope

- **In**:
  - `tools/file.py` — delete lines 22 and 24 inside `_resolve_path`.
  - `agent.py` — delete line 1420 (`history_snapshot`) and line 720 (`summary_cfg`).
  - `tool_recovery.py` — delete line 123 (`base_url`).
  - `tests/test_no_dead_locals.py` — new regression test (see measurement method).
  - `plan/CICD/improvements/0013-dead-locals.md` (this file) + `.results.md` + `plan/CICD/progress.md` row.
- **Out**:
  - Repo-wide dead-local scan. Scope is the 5 already-found hits + their functions; we do not expand the guard to every file in the repo this cycle.
  - Any logic changes to `_resolve_path`. The function's semantics stay identical — the `if path.startswith(str(cwd)[1:])` branch remains, and so does the `return p` fallback. Only the two assigned-but-unused locals leave.
  - Dead imports, dead functions, dead classes. Only unused locals inside function bodies.
  - Touching other `agent.py` / `tool_recovery.py` functions beyond the three listed.

## Implementation steps

1. Delete `tools/file.py` lines 22 and 24. The function body shrinks from ~17 lines to ~15 lines; the remaining flow (`if not p.is_absolute(): try: if path.startswith(...): return Path("/" + path); except: pass; return p`) is unchanged. Verify with a tiny sanity call: `_resolve_path("relative/thing.py")` still returns `Path("relative/thing.py")`.
2. Delete `agent.py:1420` (`history_snapshot = len(conversation_history)`). Function is long; make sure nothing later references the name (already verified by grep: sole hit is the assignment itself).
3. Delete `agent.py:720` (`summary_cfg = self._config.get("summary", {})`) inside `_worker`. Already verified nothing downstream reads it.
4. Delete `tool_recovery.py:123` (`base_url = ...`) inside `_ask_for_param`. The following line (`model = ...`) is kept because it IS used in the `json=` payload.
5. Write `tests/test_no_dead_locals.py`. Strategy: a `KNOWN_FUNCS` list of `(path, func_name)` tuples; for each entry, `ast.parse` the file, find the function(s) with that name, walk their bodies, collect `Name(Store)` IDs, subtract `Name(Load)` IDs, subtract function args, subtract common false positives (e.g. augmented-assignment targets — `x += 1` counts as both Store and Load; loop vars; walrus — but we'll re-add these only if a false positive surfaces). Assert the remaining set is empty per function.
6. Run the full unittest discovery. Fix any test failures; debug-to-green loop.
7. Write `plan/CICD/improvements/0013-dead-locals.results.md` (baseline 5 → after 0, tests 120 → 121, verdict).
8. Append the 0013 row to `plan/CICD/progress.md`.
9. Final commit on the branch: `CICD 0013 (#24): record cycle 0013 plan, results, and progress log`. Body includes `Closes #24`.

Commit layout (small, reviewable chunks):

- **Commit 1** — `CICD 0013 (#24): delete dead local assignments in tools/file.py, agent.py, tool_recovery.py` (the 5 deletions).
- **Commit 2** — `CICD 0013 (#24): regression test — no dead local assignments in touched functions` (new `tests/test_no_dead_locals.py`).
- **Commit 3** — `CICD 0013 (#24): record cycle 0013 plan, results, and progress log`. (`Closes #24` in body.)

## Test plan

- **Existing tests that must stay green** — all 120 tests in `tests/`. The deletions are in hot-path code (`_resolve_path`, `run_agent_single`, `_AsyncSummarizer._worker`, `_ask_for_param`), so any regression here would trip an existing test. Specifically:
  - `tests/test_file_tool*.py` exercises `_resolve_path` indirectly via `fn(action=..., path=...)`.
  - Other agent.py runtime tests will exercise `run_agent_single` / `_AsyncSummarizer` (if covered; if not, manual sanity check by importing agent.py is enough — the code paths are byte-identical modulo dead-assignment removal).
  - `tool_recovery.py` is covered by its own tests if present; otherwise the import-time check is enough.
- **New tests I'll add**:
  - `tests/test_no_dead_locals.py` — walks `tools/file.py::_resolve_path`, `agent.py::run_agent_single`, `agent.py::_worker` (the one inside `_AsyncSummarizer.kick`), `tool_recovery.py::_ask_for_param`, and asserts each has **zero** assigned-but-never-read locals after filtering function args and augmented-assignment targets.
- **Re-run probe**: the P-bug probe from PROBE stays informational (variance too high for a small delta as cycle 0011 proved). This cycle's metric is the dead-local scan itself, not a probe metric.

## Risks & mitigations

- **Risk**: the AST scan has false positives on augmented assignments (`x += 1`), walrus operators (`while (chunk := f.read(1024))`), tuple unpacking (`a, b = foo()` where only `a` is read), or closure-captured variables.
  - **Mitigation**: the regression test targets a whitelisted set of 4 functions, not the whole repo. Any false positive in those 4 functions today would already have surfaced in the baseline scan. If the test turns out to be fragile, narrow the target set further before giving up — the absolute floor is checking only the 5 specific names we're deleting, by scanning the function body for `^\s*(resolved|cwd_str|history_snapshot|summary_cfg|base_url)\s*=` and asserting 0 matches. That's a trivial guard that cannot false-positive.
- **Risk**: deleting `summary_cfg` silently breaks a code path I didn't see.
  - **Mitigation**: I already grepped `agent.py` for `summary_cfg` — the only hit is the assignment itself. Deleting it is safe.
- **Risk**: deleting `resolved` / `cwd_str` in `_resolve_path` changes the function's timing (the `.resolve()` call is removed — no more filesystem syscall on the hot path).
  - **Mitigation**: this is a correctness-neutral speedup, not a regression. Existing tests exercising `_resolve_path` via the file tool will prove behavior is unchanged.
- **Risk**: `tests/test_no_dead_locals.py` becomes an over-eager future-maintainer annoyance.
  - **Mitigation**: keep the whitelist tight. Document in the test's docstring that expanding the whitelist is fine but new dead locals in listed functions are a hard fail.

## Rollback

Single-commit revert on the branch: `git revert <SHA>` for each of the 3 commits, or simply delete the branch before it's merged. Because the deletions are isolated and do not affect any imports or public API, a revert is fully self-contained. The worktree branch is not yet merged, so rollback is effectively free until the creator merges the PR.

## Closes

Closes #24
