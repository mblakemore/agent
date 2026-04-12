# 0029 — repeat-banner-box-drawing

**Issue**: #62 — friction: on_repeat_run_start uses ASCII = separator instead of ─ box-drawing char after cycle 0028 update
**Branch**: cicd/0029-repeat-banner-box-drawing (will be created in Phase 6)

## Goal

Replace the ASCII `=` × 60 separator in `TerminalCallbacks.on_repeat_run_start` with
`─` × 60 box-drawing chars (matching `on_session_start` after cycle 0028), add VIOLET
color to the bars and SKY bold to the label, and add 2 regression tests.

## Motivation

Cycle 0028 updated `on_session_start` to use `─` (U+2500) with `theme.c(theme.VIOLET, ...)`
coloring, creating visual hierarchy between chrome and content. `on_repeat_run_start` was not
touched by 0028 and still renders `====...` (plain ASCII, no color), making `--repeat`
run boundaries visually inconsistent with the rest of the aurora theme.
Issue: #62. Probe log: /tmp/agent-cicd/probes/0029-pcount-before.log.

## Success metric

- **Baseline**: `grep -c "'=' \* 60" callbacks.py` → **1**
- **Target**: `grep -c "'=' \* 60" callbacks.py` → **0**
- **Measurement method**:
  ```bash
  grep -c "'=' \* 60" /tmp/agent-cicd/0029-repeat-banner-box-drawing/callbacks.py
  ```
- **Secondary**: +2 new tests (test count 150 → 152)

## Scope

- **In**:
  - `callbacks.py` → `TerminalCallbacks.on_repeat_run_start` only
  - `tests/test_callbacks.py` → new `TestRepeatRunStartBanner` class with 2 tests
- **Out**:
  - `on_repeat_done` — left as-is; plain text is acceptable for the summary line
  - `NullCallbacks.on_repeat_run_start` — stub, no change needed
  - `tui.py` / `TuiCallbacks` — TuiCallbacks inherits TerminalCallbacks, so the fix is free
  - Any change to how `--repeat` works in `agent.py`

## Implementation steps

### Step 1 — Update `on_repeat_run_start` in `callbacks.py`

At `callbacks.py:223-224`, replace:
```python
def on_repeat_run_start(self, label: str) -> None:
    self._print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
```
With:
```python
def on_repeat_run_start(self, label: str) -> None:
    bar = theme.c(theme.VIOLET, "─" * 60)
    title = theme.c(theme.SKY, label, bold=True)
    self._print(f"\n{bar}\n{title}\n{bar}")
```

This exactly mirrors the `on_session_start` bar+title pattern, using the same variables.

### Step 2 — Add regression tests in `tests/test_callbacks.py`

Add a new class `TestRepeatRunStartBanner` after the existing `TestSessionStartBanner` class:

```python
class TestRepeatRunStartBanner(unittest.TestCase):
    """Source-level regression guards for on_repeat_run_start banner style."""

    def _src(self):
        import inspect
        return inspect.getsource(
            callbacks.TerminalCallbacks.on_repeat_run_start
        )

    def test_repeat_run_start_uses_box_drawing_char(self):
        """Horizontal bars should use U+2500 ─ rather than plain ASCII = characters."""
        self.assertIn("─", self._src(),
                      "on_repeat_run_start bars must use ─ (U+2500), not = ")

    def test_repeat_run_start_uses_sky_color(self):
        """The run label should be rendered in SKY color for visual hierarchy."""
        self.assertIn("theme.SKY", self._src(),
                      "on_repeat_run_start must apply theme.SKY to the label")
```

## Test plan

- Existing tests that must stay green: all 150
- New tests (2):
  1. `TestRepeatRunStartBanner.test_repeat_run_start_uses_box_drawing_char` — source asserts `─`
  2. `TestRepeatRunStartBanner.test_repeat_run_start_uses_sky_color` — source asserts `theme.SKY`
- Re-run probe: P-count — expected unchanged wall time and correct count (150)

## Risks & mitigations

- **NO_COLOR path**: `theme.c()` passes through to plain text when NO_COLOR is set. The `─`
  character is structural (not an escape), so it still appears in the output — safe.
- **TuiCallbacks inherit**: `TuiCallbacks` inherits `TerminalCallbacks.on_repeat_run_start`
  unchanged; no TUI-specific override needed.
- **`visible_len` in toolbar**: `on_repeat_run_start` is not involved in toolbar rendering,
  so no char-width calculation is affected.

## Rollback

The change is confined to 4 lines in `callbacks.py` and 2 new tests in `tests/test_callbacks.py`.
Rollback is `git revert` of the single commit on the branch, or simply discarding the branch.

## Closes

Closes #62
