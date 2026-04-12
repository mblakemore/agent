# 0039 — think-raw-print

**Issue**: #81 — bug: tools/think.py uses raw print() for streaming output, bypassing callback system (D12 violation)
**Branch**: cicd/0039-think-raw-print (will be created in Phase 6)

## Goal

Remove the 4 raw `print()` calls from `tools/think.py` and route think output
through the agent callback system via an injectable `_output` module variable.

## Motivation

`tools/think.py` lines 117–119 and 124–125 call `print()` directly, violating
the D12 invariant that all UI output flows through the callback system. In TUI
mode this raw `print()` can corrupt the `prompt_toolkit` prompt line. It also
makes think output untestable in isolation.

This is the same class of bug fixed in cycle 0035 for `tool_recovery.py`.

## Success metric

- Baseline: `grep -c 'print(' tools/think.py` = 4
- Target: 0
- Measurement method: `grep -c 'print(' <worktree>/tools/think.py`

## Scope

- In: `tools/think.py` (replace `print()` with `_output()`), `agent.py`
  (inject callback-aware `_output` into think module), `tests/` (regression test)
- Out: `callbacks.py` interface changes (no new callback methods needed)

## Implementation steps

1. **tools/think.py**: Add module-level `_output = print` variable. Replace
   all 4 `print(...)` calls with `_output(...)`.

2. **agent.py**: After the `_cb` global is set up (near line 57), add a lazy
   injection. The injection must happen after `run_agent_single` sets `_cb`,
   so the right place is inside `run_agent_single` after `_cb = cb if cb is
   not None else TerminalCallbacks(verbose=verbose)` (line ~1196). Add:
   ```python
   import tools.think as _think_mod
   _think_mod._output = lambda text: _emit("on_stream_chunk", text)
   ```
   This mirrors the pattern used by `StreamFilter` which takes a `writer`
   callable (`lambda t: _emit("on_stream_chunk", t)`).

3. **tests/test_think_no_print.py**: New file with:
   - `test_no_raw_print_in_think_source`: static assertion that `print(` does
     not appear in `tools/think.py` outside comments.
   - `test_think_output_uses_injectable_fn`: assert `tools.think._output` is
     a module-level attribute (i.e., the injectable is present), and that
     replacing it with a mock captures the output strings without calling
     `builtins.print`.

## Test plan

- Existing tests: all 185 currently passing must stay green
- New tests in `tests/test_think_no_print.py`: 2 tests (static + behavioral)
- Re-run metric: `grep -c 'print(' tools/think.py` must equal 0

## Risks & mitigations

- **Risk**: the `_output` injection happens inside `run_agent_single`, which
  means direct calls to `tools.think.fn()` in tests will still use `print()`.
  **Mitigation**: this is acceptable — the `_output` default is `print`, so
  standalone use still works. The D12 fix kicks in when the full agent is
  running (which is the only path that triggers TUI corruption).
- **Risk**: if `think.py` is imported before `run_agent_single` sets `_cb`,
  the injection hasn't happened yet. **Mitigation**: the think tool is only
  invoked during the agent loop (after `run_agent_single` has initialized),
  so the injection always precedes the first call.

## Rollback

Delete the worktree branch; the parent `main` checkout is untouched.

## Closes

Closes #81
