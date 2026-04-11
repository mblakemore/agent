# 0017 — callbacks-surface-signal — results

- Issue: #38
- Branch: cicd/0017-callbacks-surface-signal
- PR: (pending — opened in TRACK)
- Commit range: `881696f..HEAD` on `cicd/0017-callbacks-surface-signal`
- Date: 2026-04-11

## Metric

- **Baseline**: 4 — tokens from `{"streaming", "exec_command", "3", "5"}`
  absent from stdout when `TerminalCallbacks.{on_cancelled, on_forced_think,
  on_text_loop_detected}` are invoked with those args on HEAD `bb50f46`.
- **After**: 0 — every token appears in the emitted lines on this branch.
- **Delta**: −4 (−100%)
- **Measurement**:

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

  Before output:
  ```

  [cancelled]
    [loop detected — forcing think]
    [text loop detected — stopping]
  ```

  After output:
  ```

  [cancelled — streaming]
    [loop detected on exec_command x3 — forcing think]
    [text loop detected — same output x5, stopping]
  ```

## Test suite

- Before: 126 passing
- After:  127 passing  (+1 regression test — `test_signal_args_surface_in_output`)

## Probe re-run

- No live probe. Metric is a pure source/output assertion covered directly by
  the new regression test, which runs as part of the full suite in <2 s.

## What I actually changed

- `callbacks.py:308-309` — `on_forced_think` body now embeds `tool_name` and
  `count` in the printed line.
- `callbacks.py:325-326` — `on_text_loop_detected` body now embeds `count`.
- `callbacks.py:365-366` — `on_cancelled` body now embeds `where` inside the
  amber colour wrapping.
- `tests/test_callbacks.py` — new `TestTerminalCallbacks.test_signal_args_surface_in_output`
  guards all three methods against future regressions via a captured-`_print`
  list and a four-token substring assertion.

Signatures and `NullCallbacks` stubs are unchanged. No `_emit` call sites in
`agent.py` were touched.

## What I learned

- The "dead argument" pattern from cycle 0016 has a sibling pattern:
  *threaded-but-discarded* arguments. Where 0016's right move was removal
  (the param truly had no job), here the right move was surfacing (the param
  was already computed by the caller and paid for; the UI just wasn't using
  it). Worth scanning for more of the same shape next cycle — any hook whose
  `log.warning` sibling prints more info than the UI is a candidate.
- Checking that no other file greps the literal placeholder string before
  editing (cycle 0010-style doc-drift guard) cost me thirty seconds and
  removed a whole class of regression risk.
