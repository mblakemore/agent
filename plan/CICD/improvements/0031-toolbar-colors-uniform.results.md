# 0031 — toolbar-colors-uniform — Results

**Issue**: #66
**Branch**: cicd/0031-toolbar-colors-uniform
**Date**: 2026-04-12
**Verdict**: PASS

## Metric

| | Before | After | Delta |
|---|---|---|---|
| `_VIOLET_HEX` refs in `bottom-toolbar*` Style entries + `_toolbar()` HTML | 18 | 0 | −18 (−100%) |
| `_BAR_BG_HEX`/`_BAR_FG_HEX` refs in tui.py | 0 | 20 | +20 |

The 2 surviving `_VIOLET_HEX` refs in `tui.py` are:
1. `"prompt"` key in `_build_style()` — prompt chevron color, intentionally unchanged
2. `"completion-menu.completion"` — autocomplete dropdown, intentionally unchanged

## Changes

`tui.py`:
- Added `_BAR_BG_HEX = "#323232"` and `_BAR_FG_HEX = "#dedede"` constants
- All 8 `bottom-toolbar*` Style entries now use `_BAR_BG_HEX` bg and `_BAR_FG_HEX` fg
  (separators use `#606060`; verbose-off state uses `#606060` for visual dimming)
- All 9 `<style>` tags in `_toolbar()` HTML now use `_BAR_BG_HEX`/`_BAR_FG_HEX`
- Trailing pad element uses `_BAR_BG_HEX`

## Test results

```
Ran 153 tests in 2.044s
OK
```

No tests needed modification — `tests/test_tui.py::TestTuiSessionToolbar` asserts
`HTML` type and `│` presence, not specific colors.

## Probe

Static grep — no live agent run. Probe log: `/tmp/agent-cicd/probes/0031-toolbar-colors-*.log`

## Visual effect

The bottom toolbar now renders as a uniform dark-gray (`#323232`) bar with off-white
(`#dedede`) text for all segments. Separators are `#606060` (subtle but visible).
`verbose off` state uses `#606060` fg for visual distinction from active segments.
The multi-color saturated palette (sky/mint/amber/violet clash) is gone.

## Reviewer note (R-0012)

Independent verification with plan's measurement command (`awk 'NR>=108 && NR<=295' tui.py | grep -c '_VIOLET_HEX'`) gives **20 before → 2 after**, not 18 → 0 as claimed in PR body and results table. The 2 survivors are `"prompt"` (line 116) and `"completion-menu.completion"` (line 125) — both intentional, documented above. The toolbar-specific count (18 → 0) is correct; the plan's measurement window was wider than the plan's own prose description anticipated (it mentioned only one survivor, not two). Implementation verified correct; improvement is real.
