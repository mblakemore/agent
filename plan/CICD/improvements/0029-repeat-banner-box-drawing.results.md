# 0029 — repeat-banner-box-drawing — results

- Issue: #62
- Branch: cicd/0029-repeat-banner-box-drawing
- PR: (pending)
- Commit range: d286903
- Date: 2026-04-12

## Metric

- Baseline: 1 (`grep -c "'=' \* 60" callbacks.py` in parent checkout)
- After:    0 (`grep -c "'=' \* 60" callbacks.py` in worktree)
- Delta:    −1 (−100%)

## Test suite

- Before: 150 passing (parent checkout baseline)
- After:  152 passing (+2 new regression tests)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0029-pcount-after.log
- Verdict: PASS (correct count 150, same 1 tool call, same wall time ~3s)

## What I actually changed

- `callbacks.py::on_repeat_run_start`:
  - Replaced `f"\n{'=' * 60}\n{label}\n{'=' * 60}"` with `bar+title+bar` pattern
  - Bars: `theme.c(theme.VIOLET, "─" * 60)` — VIOLET colored, box-drawing char (U+2500)
  - Label: `theme.c(theme.SKY, label, bold=True)` — SKY bold, matching on_session_start title style
  - Net: 1 line → 3 lines, fully consistent with on_session_start (cycle 0028 design)

- `tests/test_callbacks.py`: added `TestRepeatRunStartBanner` class (2 tests):
  - `test_repeat_run_start_uses_box_drawing_char` — source asserts `─` in on_repeat_run_start
  - `test_repeat_run_start_uses_sky_color` — source asserts `theme.SKY` in on_repeat_run_start

## What I learned

- When a UI pattern is established (like the bar+title structure from 0028), immediately audit
  sibling callbacks that render similar chrome — catching inconsistencies in the same cycle is
  cheaper than filing a follow-up.
- Source-level assertions are robust for visual design invariants: they work without a TTY,
  run in under 0.01s, and catch any regression regardless of rendering environment.
- The worktree test count (152) and the P-count probe result (150) differ because the probe
  counts `def test_` in the *parent* checkout; this is expected and not a discrepancy.
