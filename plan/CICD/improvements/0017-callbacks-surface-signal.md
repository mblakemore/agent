# 0017 — callbacks-surface-signal

**Issue**: #38 — friction: TerminalCallbacks discards useful args on on_cancelled / on_forced_think / on_text_loop_detected
**Branch**: cicd/0017-callbacks-surface-signal

## Goal

Make `TerminalCallbacks` surface the per-event signal it already receives from
`_emit(...)` for three hooks whose bodies currently throw their arguments away.

## Motivation

Static scan from cycle 0017 PERCEIVE found three `TerminalCallbacks` methods
whose bodies print a fixed placeholder string and ignore every non-`self`
argument:

| Callback | Dead args | Current output |
|---|---|---|
| `on_cancelled(where)` | `where` ("streaming" / "tool_execution") | `[cancelled]` |
| `on_forced_think(tool_name, count)` | both | `[loop detected — forcing think]` |
| `on_text_loop_detected(count)` | `count` | `[text loop detected — stopping]` |

The `log.warning` siblings at `agent.py:1616`, `agent.py:1901-1902`, and
`agent.py:1647-1648` already record the useful values, so the UI is strictly
less informative than the log for the same events. Fixing this cost the same
emit call sites nothing — the values are already threaded through `_emit`.

## Success metric

- **Baseline**: **4** — number of tokens from `{"streaming", "exec_command", "3", "5"}`
  absent from the stdout produced by invoking the three hooks on a fresh
  `TerminalCallbacks()` instance (captured in this repo at HEAD `bb50f46`).
- **Target**: **0** — every threaded arg surfaces in the callback's printed line.
- **Measurement method**: the following exact snippet (also asserted in the
  new regression test):

  ```python
  import io, contextlib, callbacks
  cb = callbacks.TerminalCallbacks()
  buf = io.StringIO()
  with contextlib.redirect_stdout(buf):
      cb.on_cancelled("streaming")
      cb.on_forced_think("exec_command", 3)
      cb.on_text_loop_detected(5)
  out = buf.getvalue()
  missing = sum(1 for t in ["streaming", "exec_command", "3", "5"] if t not in out)
  print(missing)
  ```

## Scope

- **In**: `callbacks.py` (bodies of `TerminalCallbacks.on_cancelled`,
  `TerminalCallbacks.on_forced_think`, `TerminalCallbacks.on_text_loop_detected`);
  `tests/test_callbacks.py` (new regression test).
- **Out**:
  - `NullCallbacks` base stubs — they stay no-op so the interface is
    unchanged and third-party subclasses keep working.
  - All `_emit` call sites in `agent.py` — no signature changes, so no caller
    edits are required.
  - Any tests that currently pass positional args to these hooks
    (`tests/test_callbacks.py:24` keeps working — signature is unchanged).
  - The `auto` parameter on `on_context_recovery` — it is always called with
    `True` so it is genuinely dead rather than discarded signal. Out of
    scope here; file a separate issue if it becomes worth a cycle.

## Implementation steps

1. `callbacks.py:365-366` — rewrite `TerminalCallbacks.on_cancelled(self, where)`
   body to emit `f"\n[cancelled — {where}]"` (keep leading newline, keep amber
   colour wrapping).
2. `callbacks.py:308-309` — rewrite `TerminalCallbacks.on_forced_think(self, tool_name, count)`
   body to emit `f"  [loop detected on {tool_name} x{count} — forcing think]"`.
3. `callbacks.py:325-326` — rewrite `TerminalCallbacks.on_text_loop_detected(self, count)`
   body to emit `f"  [text loop detected — same output x{count}, stopping]"`.
4. `tests/test_callbacks.py` — add one new test
   `TestTerminalCallbacks.test_signal_args_surface_in_output` that captures
   stdout via `contextlib.redirect_stdout` and asserts every token from
   `{"streaming", "exec_command", "3", "5"}` appears in the captured output.
5. Run the full suite. All 126 existing tests plus the new one must pass.

## Test plan

- **Existing tests that must stay green**: all 126 currently passing, in
  particular `tests/test_callbacks.py:TestNullCallbacks.test_all_hooks_return_none`
  (still calls `cb.on_forced_think("t", 1)` positionally — signature unchanged,
  still returns `None`).
- **New test I'll add**: `TestTerminalCallbacks.test_signal_args_surface_in_output`
  — runs the three hooks, captures stdout, asserts `missing == 0` against the
  four-token target set. Guards both directions: a future edit that reverts
  any one of the three bodies will fail the test.
- **Re-run probe**: no live probe — the metric is a pure source/output
  assertion and the signal is already captured by the regression test. The
  full unit suite takes <3 s so the verification loop stays tight.

## Risks & mitigations

- **Risk**: downstream scripts grep for the literal strings `[cancelled]`,
  `[loop detected — forcing think]`, or `[text loop detected — stopping]`.
  **Mitigation**: grepped the whole repo for each bracketed substring before
  editing; only sources are `callbacks.py` itself and one earlier-cycle
  progress row that describes (not asserts) the old wording. No test or
  runtime code pattern-matches the output.
- **Risk**: the new strings break terminal alignment in a way I don't notice
  without a live TTY.
  **Mitigation**: keep the `  ` leading indent identical, keep the amber/rose
  colour wrappers identical, only extend the bracketed payload.
- **Risk**: `on_forced_think` is called inside a tight retry loop and the
  extra formatting could spam under pathological input.
  **Mitigation**: this hook was already printing a line per call — formatting
  cost is a single f-string, no new allocation shape.

## Rollback

Single-file revert: `git revert <impl commit>` on the branch restores the
three bodies to their placeholder strings. The regression test commit is a
separate commit so it can be kept or dropped independently.

## Closes

Closes #38
