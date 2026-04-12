# 0035 — tool-recovery-raw-print — results

- Issue: #74
- Branch: cicd/0035-tool-recovery-raw-print
- PR: (pending)
- Commit range: 8203074..8203074
- Date: 2026-04-12

## Metric

- Baseline: 1 (`grep -c 'print(' tool_recovery.py` — 1 raw print() bypassing callbacks)
- After:    0
- Delta:    −1 (−100%)

## Test suite

- Before: 172 passing
- After:  174 passing (+2 new regression guards)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0035-tests-after.log
- Verdict: PASS (all 174 tests green, metric gate satisfied)

## What I actually changed

- `tool_recovery.py`: Removed `print(f"    [recovered: set {pattern['param']}={value}]")` from the `attempt_recovery` success path. The `log.info("Recovery: succeeded on attempt %d", ...)` already captures the event in the file log — the raw `print()` was added to show a visible status but violated D12 by bypassing callbacks.

- `agent.py`: Added `_emit("on_notice", "info", f"[recovered: {func_name} succeeded]")` immediately after `result_str = recovered` to surface the recovery success through `TerminalCallbacks._note` (dimmed, styled, NO_COLOR-safe, and TUI `patch_stdout`-safe).

- `tests/test_tool_recovery_no_print.py`: Two new tests:
  * `test_no_raw_print_in_tool_recovery_source` — static assertion: no `print(` in tool_recovery.py
  * `test_attempt_recovery_does_not_call_print_on_success` — behavioral: mocks map_fn and LLM, triggers a successful end_line recovery, asserts `builtins.print` is never called

## What I learned

- D12 invariant violations are easy to miss in helper modules that are imported by agent.py — they look isolated but their output reaches the same terminal and can corrupt TUI mode.
- The `on_notice` callback is the right channel for status messages from the agent loop; `log.info` handles file logging. Having both is correct — the raw `print()` was a third, rogue channel.
- Static tests (`assert 'print(' not in source_file`) are cheap and permanently prevent the pattern from re-entering.
