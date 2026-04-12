# 0028 — ui-banner-footer

**Issue**: #60 — Interactive UI Enhancement Request
**Branch**: cicd/0028-ui-banner-footer (will be created in Phase 6)

## Goal

Redesign the welcome banner in `callbacks.py::on_session_start` and the TUI
footer bar in `tui.py::TuiSession._toolbar` for improved visual clarity and
color hierarchy, as requested by the creator with an attached screenshot.

## Motivation

Issue #60 filed 2026-04-12 by mblakemore with a screenshot of the current TUI.
Creator requested "significantly improve the program welcome message, header and
status text, and the footer bar colors and appearance."

Current state (from screenshot + code reading):
- Banner title uses the same VIOLET color as the decorative bars → no visual
  hierarchy between chrome and content.
- The `=` × 60 bars are generic; the title does not stand out.
- Session info (log paths, escape hint) is an undifferentiated block of text.
- TUI footer uses plain `|` ASCII pipe separators between segments.

## Success metric

- **Metric A**: `grep -c 'theme\.SKY' <(python3 -c "import inspect,sys;sys.path.insert(0,'/path');import callbacks;print(inspect.getsource(callbacks.TerminalCallbacks.on_session_start))")` → title now uses SKY
- **Metric B**: `grep -c '│' tui.py` → footer uses Unicode box-drawing separators
- **Combined baseline**: 0 (A=0, B=0)
- **Combined target**: ≥ 5 (A ≥ 1, B ≥ 4)
- **Measurement method** (exact):
  ```bash
  cd /tmp/agent-cicd/0028-ui-banner-footer
  python3 -c "
  import inspect, sys
  sys.path.insert(0, '.')
  import callbacks
  src = inspect.getsource(callbacks.TerminalCallbacks.on_session_start)
  print('A:', src.count('theme.SKY'))
  "
  echo "B: $(grep -c '│' tui.py)"
  ```

## Scope

- **In**:
  - `callbacks.py` → `TerminalCallbacks.on_session_start` (banner redesign)
  - `tui.py` → `TuiSession._toolbar` (footer separator upgrade)
  - `tests/test_callbacks.py` → new test asserting title uses SKY
  - `tests/test_tui.py` → new test asserting toolbar uses `│`
- **Out**:
  - Any change to non-TUI paths (`--no-tui`, `NullCallbacks`)
  - Theme color definitions (`theme.py`) — we use existing palette only
  - Toolbar data (model, msgs, ctx%) — read-only pass, no new data sources
  - `tui.py::TuiCallbacks.on_session_start` — the TUI-mode note stays unchanged

## Implementation steps

### Step 1 — Banner redesign (`callbacks.py`)

In `TerminalCallbacks.on_session_start`:

1. Change bar characters: `"=" * 60` → `"─" * 60` (U+2500 BOX DRAWINGS LIGHT
   HORIZONTAL). Color stays VIOLET, bold removed (thinner visual weight).
2. Change title color: `theme.c(theme.VIOLET, ...)` → `theme.c(theme.SKY, ...)
   bold=True`. SKY (#35c2f5) stands out clearly against violet chrome bars.
3. Add a ` ›` prefix to the API health line:
   `self._print(f" › API: {health}")` — gives the status block a subtle
   indent/marker without adding new lines.
4. Dim the session log paths (they are background info):
   `self._print(theme.dim(f"   {info.get('log_path')}"))`
   `self._print(theme.dim(f"   {info.get('error_log_path')}"))`
   with a single `self._print(theme.dim("   ─── session ───"))` separator
   line before them, colored VIOLET dim.

### Step 2 — Footer separator upgrade (`tui.py`)

In `TuiSession._toolbar`, replace all five ` | ` string literals with ` │ `
(U+2502 BOX DRAWINGS LIGHT VERTICAL). Affects both the `left` f-string segments
and the `visible_len` calculation string.

Change:
```python
f'<style fg="#707070" bg="{_VIOLET_HEX}"> | </style>'
```
To:
```python
f'<style fg="#505070" bg="{_VIOLET_HEX}"> │ </style>'
```
Also update the `visible_len` calculation: `" | "` → `" │ "` (same byte width
for ASCII but we need to count the *display* width = 3 characters either way,
so no change needed — `│` is 1 wide character). The `visible_len` string uses
`" | "` directly in the bare Python string, so update it to `" │ "` for
consistency; the char widths are identical.

### Step 3 — Regression tests

**`tests/test_callbacks.py`** — add `TestSessionStartBanner` class:
- `test_title_uses_sky_color`: call `on_session_start(info_dict)` with stdout
  captured (patch `_print`); assert the title string contains the SKY escape
  code (via `theme.escape(theme.SKY, bold=True)` or check source text for
  `theme.SKY`). Simpler: assert `inspect.getsource(...)` contains `theme.SKY`
  within the function body. Use source inspection so the test is fast and
  doesn't require a TTY.
- `test_bar_uses_box_drawing_char`: source-inspect `on_session_start` and assert
  `"─"` (U+2500) appears in the function body.

**`tests/test_tui.py`** — add to `TestTuiSessionToolbar`:
- `test_toolbar_uses_unicode_separator`: call `_toolbar()` and assert `"│"` in
  `str(toolbar.value)`.

## Test plan

- Existing tests that must stay green: all 147 (full suite)
- New tests (3):
  1. `TestSessionStartBanner.test_title_uses_sky_color` — source asserts SKY in on_session_start
  2. `TestSessionStartBanner.test_bar_uses_box_drawing_char` — source asserts `─` in on_session_start
  3. `TestTuiSessionToolbar.test_toolbar_uses_unicode_separator` — rendered toolbar HTML contains `│`
- Re-run probe: P-count (non-interactive) — wall time and line count expected unchanged

## Risks & mitigations

- **`visible_len` off-by-one**: `│` is U+2502, display width 1 — same as `|`.
  No change to the calculation is needed. Verify by running a quick render check.
- **NO_COLOR path**: `theme.c()` and `theme.dim()` both pass through to plain
  text when NO_COLOR. The `─` and `│` characters appear in both color and no-color
  output — they're structural, not decorative escapes. ✓
- **Prompt_toolkit HTML escaping**: `│` is not an HTML special char — safe to
  embed in the `HTML(...)` call as-is. ✓

## Rollback

```bash
cd /mnt/droid/repos/agent
git diff HEAD~2 -- callbacks.py tui.py | git apply --reverse
```
Or simply: the worktree branch is not merged; discard it.

## Closes

Closes #60
