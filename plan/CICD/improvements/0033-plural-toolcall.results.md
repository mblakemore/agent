# 0033 — plural-toolcall — results

- Issue: #71
- Branch: cicd/0033-plural-toolcall
- PR: (to be assigned)
- Commit range: e48205e..e48205e
- Date: 2026-04-12

## Metric

- Baseline: 4 (`grep -c '(s)' callbacks.py`)
- After:    0
- Delta:    −4 (−100%)

## Test suite

- Before: 162 passing
- After:  166 passing (+4 new pluralization regression guards)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0033-pcount-before.log (baseline only — display-only change, no live re-run needed)
- Verdict: PASS (metric gate satisfied, all tests green)

## What I actually changed

- `callbacks.py`: Added `_nplural(n, singular, plural)` helper above `TerminalCallbacks`; used it in 4 places: `on_repeat_done` ("run/runs"), `on_tool_batch_start` ("tool call/tool calls"), and two branches of `render_tools` ("tool call/tool calls").
- `agent.py`: Changed `log.debug("Executing %d tool call(s)")` to `log.debug("Executing %d tool calls")`.
- `tests/test_callbacks.py`: Added 4 new tests: `test_on_tool_batch_start_singular`, `test_on_tool_batch_start_plural`, `test_render_tools_singular_header`, `test_render_tools_plural_header`.
- `tests/test_tools_paging.py`: Updated 2 assertions that pinned the old `(s)` strings (`"All 50 tool call(s)"` → `"All 50 tool calls"`, `"All 7 tool call(s)"` → `"All 7 tool calls"`).
- `tests/test_agent_console_dedup.py`: Updated 1 assertion in `BANNED_AT_INFO` list from `"Executing %d tool call(s)"` to `"Executing %d tool calls"`.

## What I learned

- Existing tests that pinned old display strings need updating alongside behavior changes — caught 3 pre-existing assertions on the `(s)` pattern that needed updating (test_tools_paging, test_agent_console_dedup).
- A small `_nplural()` helper is cleaner than inline ternaries — readable, reusable, and directly testable.
- The `(s)` pattern appeared in 5 places total, but the grep metric (`grep -c '(s)' callbacks.py`) cleanly captured 4 of them; the 5th in agent.py was a log.debug format string handled separately.
