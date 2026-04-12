# 0035 — tool-recovery-raw-print

**Issue**: #74 — bug: tool_recovery.py uses raw print() for recovery-success message, bypassing callback system (D12 violation)
**Branch**: cicd/0035-tool-recovery-raw-print (will be created in Phase 6)

## Goal

Remove the raw `print()` in `tool_recovery.py` that bypasses the callback system, and surface the recovery-success message through `_emit("on_notice", ...)` in `agent.py` instead.

## Motivation

`tool_recovery.py` line 207 calls `print(f"    [recovered: set {pattern['param']}={value}]")` directly, violating the D12 invariant that all UI output flows through the callback system. In TUI mode this raw `print()` can corrupt the rendered `prompt_toolkit` prompt line. It also ignores `NO_COLOR` and makes recovery output untestable in isolation.

Discovered in CICD cycle 0035 code inspection.

## Success metric

- Baseline: `grep -c 'print(' /mnt/droid/repos/agent/tool_recovery.py` = 1
- Target: 0
- Measurement method: `grep -c 'print(' <worktree>/tool_recovery.py`

## Scope

- In: `tool_recovery.py` (remove `print()`), `agent.py` (add `_emit("on_notice", ...)` after successful recovery), `tests/` (add regression test)
- Out: `callbacks.py` interface changes (no new callback methods needed), other tools

## Implementation steps

1. In `tool_recovery.py`, remove the `print(f"    [recovered: set {pattern['param']}={value}]")` line from `_ask_for_param` (line 207). The `log.info("Recovery: succeeded on attempt %d", ...)` already captures the event in the log file — the print was redundant.

2. In `agent.py`, after the `if recovered is not None:` block sets `result_str = recovered`, add:
   ```python
   _emit("on_notice", "info", f"[recovered: {func_name} succeeded]")
   ```
   This surfaces the recovery success through the existing `on_notice` callback which is already styled/routed correctly in `TerminalCallbacks`. The `attempt_recovery` function already calls `log.info` with the corrected param/value, so the detail is in the log — the callback message just needs to confirm success.

3. In `tests/test_tool_recovery.py` (new file):
   - `test_no_raw_print_in_tool_recovery`: static assertion that `print(` does not appear in `tool_recovery.py`
   - `test_attempt_recovery_returns_result_not_none`: behavioral — set up a mock `map_fn` and `llm_call_fn`, verify `attempt_recovery` returns a non-None result and does NOT call `builtins.print` (patch it and assert call count == 0)

## Test plan

- Existing tests that must stay green: all 172 currently passing
- New tests:
  - `tests/test_tool_recovery.py`: 2 tests (static + behavioral with mocked callbacks)
- Re-run probe: P-count (primary metric is static; probe re-run confirms no regression)

## Risks & mitigations

- **Risk**: the `on_notice` message format differs from the original `print()` output → users who rely on the exact string `[recovered: set end_line=N]` won't see it anymore. **Mitigation**: the new `on_notice` message is an acceptable substitution (`[recovered: <toolname> — retried with corrected args]`) and is styled/dimmed consistently with other notices. The exact param/value info remains in the file log via `log.info`.
- **Risk**: existing tests check for the `print()` output → none do (verified by grep). No breakage expected.

## Rollback

Delete the worktree branch; the parent `main` checkout is untouched.

## Closes

Closes #74
