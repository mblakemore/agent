# 0031 — toolbar-colors-uniform

**Issue**: #66 — friction: bottom toolbar colors clash, labels hard to read — use #323232/#dedede
**Branch**: cicd/0031-toolbar-colors-uniform (will be created in Phase 6)

## Goal

Replace the multi-color saturated toolbar palette in `tui.py` with a uniform dark-gray/light-text
design: background `#323232`, foreground `#dedede` for every segment and separator.

## Motivation

The current toolbar uses five different foreground colors (`_SKY_HEX`, `_MINT_HEX`, `_AMBER_HEX`,
`#ffffff`, `#505070`) against a single `_VIOLET_HEX` (`#7b4dff`) background. The sky+violet pairing
has low luminance contrast; the multi-color scheme makes the toolbar visually busy and hard to scan.

The fix is: add two new palette constants `_BAR_BG_HEX = "#323232"` and `_BAR_FG_HEX = "#dedede"`,
then apply them uniformly to every `bottom-toolbar*` entry in `_build_style()` and every `<style>`
tag in `_toolbar()`. The verbose segment's color toggle (mint/gray) becomes `_BAR_FG_HEX`/`#606060`
for the off state — still distinguishable but within the neutral palette.

## Success metric

`awk 'NR>=108 && NR<=295' tui.py | grep -c '_VIOLET_HEX'`
- Before: 20
- Target: 0

(Every toolbar/style use of `_VIOLET_HEX` is replaced by `_BAR_BG_HEX` or removed. The one
surviving `_VIOLET_HEX` reference — `"prompt"` key in `_build_style` at line 110 — stays, as
the prompt chevron color is unrelated to the toolbar.)

## Probe

Static grep — no live agent run required. Check that:
1. `awk 'NR>=108 && NR<=295' tui.py | grep -c '_VIOLET_HEX'` == 0
2. `grep -c '_BAR_BG_HEX\|_BAR_FG_HEX' tui.py` >= 4 (at least the definition line + a few uses)
3. Test suite (`python3 -m unittest discover tests`) stays green.

## Implementation plan

### 1. Add palette constants (after existing Aurora hex constants, ~line 74)

```python
_BAR_BG_HEX = "#323232"
_BAR_FG_HEX = "#dedede"
```

### 2. Update `_build_style()` bottom-toolbar entries (lines 111–118)

Replace every `bg:{_VIOLET_HEX}` in the `bottom-toolbar*` dict entries with `bg:{_BAR_BG_HEX}`,
and replace each segment's foreground color with `{_BAR_FG_HEX}`. Specifically:

```python
"bottom-toolbar":                     f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
"bottom-toolbar.cwd":                 f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX} bold",
"bottom-toolbar.sep":                 f"bg:{_BAR_BG_HEX} #606060",
"bottom-toolbar.model":               f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
"bottom-toolbar.msgs":                f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
"bottom-toolbar.ctx":                 f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
"bottom-toolbar.verbose-on":          f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX} bold",
"bottom-toolbar.verbose-off":         f"bg:{_BAR_BG_HEX} #606060",
```

`completion-menu` entries are unrelated to the toolbar — keep as-is.

### 3. Update `_toolbar()` inline HTML (lines 276–294)

Replace every `bg="{_VIOLET_HEX}"` → `bg="{_BAR_BG_HEX}"` and each segment's `fg="{colour}"` →
`fg="{_BAR_FG_HEX}"`. Separators (`│`) use `fg="#606060"` for a subtle but still visible divider.
The verbose `_MINT_HEX if verbose else "#909090"` conditional becomes
`_BAR_FG_HEX if verbose else "#606060"`.

### 4. Trailing pad line (line 294)

`bg="{_VIOLET_HEX}"` → `bg="{_BAR_BG_HEX}"`.

### 5. Cleanup: check whether `_VIOLET_HEX` is still used anywhere outside toolbar scope

`grep -n '_VIOLET_HEX' tui.py` — the `"prompt"` key (line 110) still uses it for the input
prompt chevron. Keep the constant; do not delete it.

## Risks

- Visual-only change: no logic altered, no data path changed.
- The existing `tests/test_tui.py::TestTuiSessionToolbar` asserts `HTML` type and `│` presence —
  neither assertion depends on colors, so no test changes are needed.
- The `_SKY_HEX`, `_MINT_HEX`, `_AMBER_HEX` constants remain defined (used by completion-menu
  and potentially by future features); do not delete them.

## Gap-fill checklist

- [x] Metric is countable before and after (grep count)
- [x] Tests: none need changing — color strings are not asserted anywhere in tests/
- [x] Constants `_SKY_HEX` / `_MINT_HEX` / `_AMBER_HEX` not deleted (completion-menu still uses `_MINT_HEX`)
- [x] `_VIOLET_HEX` kept for prompt chevron
- [x] New constants placed in the existing palette block at lines 70-74
