# 0033 — plural-toolcall

**Issue**: #71 — friction: callbacks.py uses (s) pseudo-plural — 'tool call(s)' and 'run(s)' should use proper English plural
**Branch**: cicd/0033-plural-toolcall (will be created in Phase 6)

## Goal

Replace every `word(s)` lazy-plural pattern in `callbacks.py` (4 occurrences) and `agent.py` (1 occurrence) with proper English pluralization so rendered output reads naturally.

## Motivation

The P-count probe log (0033-pcount-before.log) shows `"Executing 1 tool call(s)..."` in every session. The `(s)` suffix is a template placeholder that was never replaced — it looks unpolished and appears in every non-empty agent run. All five occurrences are in display-only paths (no behaviour change).

Issue: #71
Probe log: /tmp/agent-cicd/probes/0033-pcount-before.log

## Success metric

- **Baseline**: `grep -c '(s)' /mnt/droid/repos/agent/callbacks.py` outputs **4**
- **Target**: outputs **0**
- **Measurement method**:
  ```bash
  grep -c '(s)' callbacks.py
  ```
  Must print `0` after the fix. Also: `grep -c '(s)' agent.py` → reduces from its baseline by 1.

## Scope

- **In**: `callbacks.py` — 4 occurrences in `on_repeat_done`, `on_tool_batch_start`, `render_tools`
- **In**: `agent.py` — 1 occurrence in `run_agent_single` (log.debug line)
- **In**: `tests/test_callbacks.py` — add assertions that rendered strings use correct singular/plural
- **Out**: no other files; no change to any logic or test fixtures

## Implementation steps

1. Add a module-level helper `_nplural(n: int, singular: str, plural: str) -> str` in `callbacks.py` that returns `f"{n} {singular}"` when `n == 1`, else `f"{n} {plural}"`. Place it just before the `NullCallbacks` class definition.

2. Replace the 4 `(s)` strings in `callbacks.py`:
   - `callbacks.py:229`: `f"Stopped after {runs} run(s)."` → `f"Stopped after {_nplural(runs, 'run', 'runs')}."` (inside `on_repeat_done`)
   - `callbacks.py:263`: `f"\nExecuting {count} tool call(s)..."` → `f"\nExecuting {_nplural(count, 'tool call', 'tool calls')}..."` (inside `on_tool_batch_start`)
   - `callbacks.py:357`: `f"All {total} tool call(s):"` → `f"All {_nplural(total, 'tool call', 'tool calls')}:"` (inside `render_tools`)
   - `callbacks.py:361`: `f"Last {shown} of {total} tool call(s):"` → `f"Last {shown} of {_nplural(total, 'tool call', 'tool calls')}:"` (inside `render_tools`)

3. Replace the 1 `(s)` string in `agent.py`:
   - `agent.py:1750`: `log.debug("Executing %d tool call(s)", len(tool_calls))` → `log.debug("Executing %d tool calls", len(tool_calls))` (this is a log.debug format string — no pluralization needed since it's debug-only and the format string is not user-visible output; just drop the `(s)`)

4. Add tests in `tests/test_callbacks.py`:
   - `test_on_tool_batch_start_singular`: capture `_print` output for `on_tool_batch_start(1)` and assert `"1 tool call..."` is in output (no `(s)`)
   - `test_on_tool_batch_start_plural`: capture for `on_tool_batch_start(3)`, assert `"3 tool calls..."` in output
   - `test_render_tools_singular_header`: add 1 tool result to history, call `render_tools()`, assert header contains `"1 tool call"` not `"1 tool call(s)"`
   - `test_render_tools_plural_header`: add 3 tool results, call `render_tools()`, assert `"3 tool calls"` in header

## Test plan

- Existing tests that must stay green: all 162 (run with `python3 -m unittest discover tests`)
- New tests: 4 methods in `tests/test_callbacks.py` (as above)
- No live-probe re-run needed (display-only change)

## Risks & mitigations

- **Risk**: The `_nplural` helper is module-level in `callbacks.py` — it could be picked up by linters as unused if the function is placed oddly.
  **Mitigation**: Place it just before the class definition, with a clear docstring. It is called from 4 places so linters won't flag it.
- **Risk**: `grep -c '(s)'` might also match other `(s)` occurrences unrelated to pluralization in `callbacks.py` (e.g. in comments or type annotations).
  **Mitigation**: Before writing the fix, verify there are exactly 4 `(s)` occurrences and they are all the plural patterns. (Verified: L229, L263, L357, L361 — all are `word(s)` plural patterns.)
- **Risk**: The `render_tools` `"Last {shown} of {total} tool call(s):"` uses `total` for the plural decision but `shown` for the count label. After fix: `_nplural(total, ...)` must use `total` not `shown` since the phrase is "N of M tool calls".
  **Mitigation**: The fix uses `total` for the plural form, matching the current intent. No behaviour change.

## Rollback

`git revert <commit>` on the single commit. The parent checkout is never touched.

## Closes

Closes #71
