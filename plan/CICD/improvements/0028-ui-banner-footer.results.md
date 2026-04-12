# 0028 — ui-banner-footer — results

- Issue: #60
- Branch: cicd/0028-ui-banner-footer
- PR: (pending)
- Commit range: 1e90b05
- Date: 2026-04-12

## Metric

- Baseline: 0 (theme.SKY in on_session_start=0, │ in tui.py=0)
- After:    6 (theme.SKY in on_session_start=1, │ in tui.py=5)
- Delta:    +6 (target was ≥5)

## Test suite

- Before: 147 passing
- After:  150 passing (+3 new regression guards)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0028-pcount-after.log
- Verdict: PASS (correct count 147, same wall-time, no regressions)

## What I actually changed

- `callbacks.py::on_session_start`:
  - Title color: VIOLET → SKY bold (visible contrast between title and chrome bars)
  - Bar character: `=` × 60 → `─` × 60 (U+2500, lighter visual weight)
  - Bar style: bold removed (bars recede, title advances)
  - API status line: `"API: {health}"` → `" › API: {health}"` (subtle ` ›` prefix)
  - Context line: `"Context size: N tokens | Max turns: M"` → `"   Context: N tokens · M max turns"` (indented, mid-dot separator)
  - Log paths: now dimmed and indented without labels (background info recedes)

- `tui.py::TuiSession._toolbar`:
  - All 4 `" | "` segment separators → `" │ "` (U+2502, #505070 on violet bg)
  - `visible_len` calculation string updated to match (same display width)

- `tests/test_callbacks.py`: added `TestSessionStartBanner` with 2 source assertions
- `tests/test_tui.py`: added `test_toolbar_uses_unicode_separator` to existing class

## What I learned

- The `─` and `│` box-drawing characters are safe across the whole stack: theme.c()
  passes them through unchanged in NO_COLOR mode, prompt_toolkit HTML accepts them
  without escaping, and the `visible_len` display-width calculation is unchanged.
- Source-level assertions are a lightweight way to lock in visual design decisions
  without needing a TTY or rendering engine — check counts of specific tokens in
  `inspect.getsource()` rather than capturing terminal output.
- Dimming secondary info (log paths) while accenting primary info (title with SKY)
  creates clearer visual hierarchy without adding new lines or changing structure.
