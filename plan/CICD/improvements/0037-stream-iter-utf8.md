# 0037 — stream-iter-utf8

**Issue**: #70 — friction: fix special character display
**Branch**: cicd/0037-stream-iter-utf8 (will be created in Phase 6)

## Goal

Fix multi-byte UTF-8 characters (emojis, non-Latin scripts, smart punctuation) being corrupted
in streamed LLM responses by replacing `iter_lines(decode_unicode=True)` with manual UTF-8 decoding.

## Motivation

Cycle 0034 (`stream-unicode-passthrough`) removed `_sanitize_display` from `_ReasoningRenderer`,
which fixed smart punctuation corruption at the Python level. However, issue #70 remains open
because the real root cause is upstream: `response.iter_lines(decode_unicode=True)` in
`agent.py:1572` uses `requests`' built-in Unicode decoding, which defaults to ISO-8859-1 for
`text/*` Content-Types without an explicit charset — and `llama.cpp` returns
`Content-Type: text/event-stream` with no charset.

Verified empirically:
- `response.encoding` = `'ISO-8859-1'` for the llama.cpp streaming endpoint
- `ð\x9f\x8c\x9f` (seen in probe logs) = 🌟 (U+1F31F) UTF-8 bytes decoded as Latin-1
- `iter_lines(decode_unicode=True)` calls `response.encoding` internally — Latin-1 decode corrupts
  all multi-byte sequences

Fix: change `iter_lines(decode_unicode=True)` → `iter_lines()` (returns raw `bytes`) and decode
each line manually as `line.decode('utf-8')`.

Probe log: `/tmp/agent-cicd/probes/0037-P-count-before.log`

## Success metric

- Primary: `grep -c 'iter_lines(decode_unicode=True)' agent.py` — baseline 1, target 0
- Secondary: `test_stream_unicode_iter.py` assertions pass (new file)
- Tertiary: test count 178 → 180+ (new regression guards)

Measurement: `grep -c 'iter_lines(decode_unicode=True)' /tmp/agent-cicd/0037-stream-iter-utf8/agent.py`

## Scope

- In: `agent.py` (one-line change at line 1572)
- In: `tests/test_stream_unicode.py` or new `tests/test_stream_iter_utf8.py` (new tests)
- Out: non-streaming path (`response.json()`), summary model call, tool_recovery.py

## Implementation steps

1. In `agent.py`, change:
   ```python
   for line in response.iter_lines(decode_unicode=True):
   ```
   to:
   ```python
   for raw_line in response.iter_lines():
       line = raw_line.decode('utf-8') if isinstance(raw_line, bytes) else raw_line
   ```

2. Add regression tests in `tests/test_stream_iter_utf8.py`:
   - Static assertion: `iter_lines(decode_unicode=True)` is absent from `agent.py`
   - Static assertion: `iter_lines()` call is present in `agent.py` (ensuring the decode is manual)
   - Static assertion: UTF-8 decode is present nearby (`.decode('utf-8')` in agent.py)
   - Behavioral: simulate the corruption scenario — show that bytes decoded as UTF-8 preserve emojis

## Test plan

- Existing tests: all 178 must stay green
- New tests:
  - `test_stream_iter_utf8.py`:
    - `test_iter_lines_no_decode_unicode` — static: `decode_unicode=True` absent from agent.py
    - `test_iter_lines_has_utf8_decode` — static: `.decode('utf-8')` present in agent.py
    - `test_utf8_bytes_round_trip` — behavioral: emoji bytes decoded as UTF-8 stay intact
    - `test_latin1_decode_corrupts_emoji` — documents the bug: same bytes decoded as latin-1 are wrong

## Risks & mitigations

- **Risk**: Some SSE lines might already be `str` not `bytes` in edge cases (e.g., mocked responses).
  → **Mitigation**: Use `isinstance(raw_line, bytes)` guard — pass through str lines unchanged.
- **Risk**: Partial multi-byte sequences split across SSE chunks.
  → **Mitigation**: SSE frames are newline-terminated JSON; partial UTF-8 in a single SSE line is not
    possible in practice. `iter_lines()` reassembles lines from chunks before yielding.

## Rollback

`git revert <commit>` — the change is a 2-line swap in one spot. Revert restores `decode_unicode=True`.

## Closes

Closes #70
