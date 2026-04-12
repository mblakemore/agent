# 0034 — stream-unicode-passthrough — results

- Issue: #70
- Branch: cicd/0034-stream-unicode-passthrough
- PR: (pending)
- Commit range: f353349..f353349
- Date: 2026-04-12

## Metric

- Baseline: 3 (`grep -c '_sanitize_display' agent.py` — 1 def + 2 call sites)
- After:    0
- Delta:    −3 (−100%)

Secondary metrics also 0:
- `_UNICODE_MAP` occurrences: 3 → 0
- `def _sanitize` occurrences: 1 → 0

## Test suite

- Before: 166 passing
- After:  172 passing (+6 new Unicode passthrough regression guards)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0034-tests-after.log
- Verdict: PASS (all 172 tests green, metric gates satisfied)

## What I actually changed

- `agent.py`: Removed `_UNICODE_MAP` (10-entry str.maketrans dict), `_sanitize` (dead function — was replaced piecemeal by `_sanitize_display` and direct `_THINK_TAG_RE.sub`), and `_sanitize_display` (applied the map to all streamed LLM text). Updated `_ReasoningRenderer._emit_plain` and `_emit_think` to pass text directly to `self._write` without transformation.
- `tests/test_stream_unicode.py`: 6 new tests — 4 behavioural (em-dash, smart quotes, ellipsis, bullet pass through `_ReasoningRenderer`) + 2 static (assert `_sanitize_display` and `_UNICODE_MAP` absent from `agent.py`).

## What I learned

- The `_sanitize` function had been dead for some time — its think-tag stripping was moved to a direct `_THINK_TAG_RE.sub()` call in the main loop, and the Unicode replacement was split off into `_sanitize_display`. Neither called `_sanitize` but neither removed it either.
- `_UNICODE_MAP` was an ASCII-downgrade applied to every streamed chunk. Removing it means em-dashes, smart quotes, and bullets now render as the model intended. On modern UTF-8 terminals this is strictly better.
- Static-check tests (`grep -c X agent.py == 0`) are a lightweight way to prevent accidental re-introduction of removed patterns.
