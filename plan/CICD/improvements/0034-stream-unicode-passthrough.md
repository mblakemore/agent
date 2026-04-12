# 0034 — stream-unicode-passthrough

**Issue**: #70 — friction: fix special character display
**Branch**: cicd/0034-stream-unicode-passthrough (will be created in Phase 6)

## Goal

Remove the `_sanitize_display` ASCII-downgrade from `_ReasoningRenderer` so streamed LLM output preserves Unicode punctuation (em-dashes, smart quotes, bullets) as the model intended. Also remove the now-dead `_sanitize` function and `_UNICODE_MAP` constant.

## Motivation

`_ReasoningRenderer._emit_plain` and `_emit_think` both call `_sanitize_display(text)` before writing each chunk to the display. `_sanitize_display` applies `_UNICODE_MAP` which replaces:
- `\u2014` (—) → `--`
- `\u2013` (–) → `-`
- `\u2018` (') → `'`
- `\u2019` (') → `'`
- `\u201c` (") → `"`
- `\u201d` (") → `"`
- `\u2026` (…) → `...`
- `\u2022` (•) → `*`
- `\u00a0` (NBSP) → ` `
- `\u200b` (ZWSP) → ``

Every character the model writes with stylistic intent (em-dash, smart quotes, ellipsis) is silently converted to an ASCII approximation. Modern terminals handle Unicode correctly; this ASCII downgrade is a misfeature.

Additionally, `_sanitize` (line 233) is a dead function — it was replaced piecemeal by `_sanitize_display` (for rendering) and direct `_THINK_TAG_RE.sub('', ...)` (in the main loop). Removing `_sanitize` eliminates dead code, and removing `_UNICODE_MAP` (which becomes orphaned once `_sanitize_display` is gone) removes the last remnant of the ASCII-downgrade design.

Issue: #70
Probe log: /tmp/agent-cicd/probes/0034-P-count-before.log

## Success metric

- **Baseline**: `grep -c '_sanitize_display' /mnt/droid/repos/agent/agent.py` outputs **3** (1 def + 2 call sites)
- **Target**: outputs **0**
- **Measurement method**:
  ```bash
  grep -c '_sanitize_display' agent.py
  ```
  Must print `0` after the fix. Also:
  - `grep -c '_UNICODE_MAP' agent.py` must drop from 3 to 0
  - `grep -c 'def _sanitize\b' agent.py` must drop from 1 to 0

## Scope

- **In**: `agent.py` — remove `_UNICODE_MAP`, `_sanitize`, `_sanitize_display`; in `_ReasoningRenderer._emit_plain` pass text directly to `self._write`; same for `_emit_think`
- **In**: `tests/test_stream_unicode.py` — new test file with regression guards
- **Out**: no change to `_sanitize_tool_args` (different function, different purpose); no change to `_THINK_TAG_RE` or its usage; no change to any other file

## Implementation steps

1. Remove `_UNICODE_MAP` block (lines 223–227) from `agent.py`.
2. Remove `_sanitize` function (lines 233–236) from `agent.py`.
3. Remove `_sanitize_display` function (lines 239–241) from `agent.py`.
4. In `_ReasoningRenderer._emit_plain` (was line 303–306): replace `self._write(_sanitize_display(text))` with `self._write(text)`.
5. In `_ReasoningRenderer._emit_think` (was line 308–311): replace `self._write(theme.dim(_sanitize_display(text)))` with `self._write(theme.dim(text))`.
6. Write `tests/test_stream_unicode.py` with:
   - `test_emit_plain_preserves_em_dash`: create `_ReasoningRenderer` with a capturing writer, feed `"text — more"`, assert output contains `"—"` (not `"--"`)
   - `test_emit_plain_preserves_smart_quotes`: feed `"\u201chello\u201d"`, assert `"\u201c"` in output
   - `test_emit_think_preserves_ellipsis`: feed think-mode text with `"\u2026"`, assert `"\u2026"` in output
   - `test_no_sanitize_display_in_agent`: static check — `grep -c '_sanitize_display' agent.py` == 0
   - `test_no_unicode_map_in_agent`: static check — `grep -c '_UNICODE_MAP' agent.py` == 0

## Test plan

- Existing tests that must stay green: all 166 (run with `python3 -m unittest discover tests`)
- New tests: `tests/test_stream_unicode.py` (5 methods as above)
- No live-probe re-run needed (display-only change — the streaming path is unchanged except for the translation being removed)

## Risks & mitigations

- **Risk**: Some downstream tests may assert streamed output contains ASCII equivalents (e.g. `"--"` instead of `"—"`). After removal, those tests would fail.
  **Mitigation**: Search all test files for `_UNICODE_MAP`, `_sanitize_display`, `"--"` near em-dash context before implementing. Fix any found assertions in the same commit.
- **Risk**: `_sanitize_display` was introduced for a reason (maybe some older terminals or model outputs that produced raw bytes). Removing it could cause issues on non-UTF-8 systems.
  **Mitigation**: Python 3's str type is always Unicode; `print()` uses `sys.stdout.encoding` to encode on output. The encoding issue (if any) is at the terminal level, not in this translation. We're removing a string-level substitution, not any encoding logic.
- **Risk**: The section comment `# ── Text utilities ─────` becomes orphaned if all utility functions are removed.
  **Mitigation**: Also remove the section comment if `_sanitize` and `_sanitize_display` are the only functions under it. `_THINK_TAG_RE` and `_sanitize_tool_args` are nearby — keep the section comment if they remain.

## Rollback

`git revert <commit>` on the single commit. The parent checkout is never touched.

## Closes

Closes #70
