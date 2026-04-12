# 0040 — think-iter-utf8

**Issue**: #83 — bug: tools/think.py uses iter_lines(decode_unicode=True) — same Latin-1 UTF-8 corruption as agent.py pre-0037
**Branch**: cicd/0040-think-iter-utf8 (will be created in Phase 6)

## Goal

Fix `tools/think.py` to decode its SSE stream as UTF-8 explicitly (matching the fix cycle 0037 applied to `agent.py`), and extend the guard test to cover `tools/think.py`.

## Motivation

Cycle 0037 fixed `agent.py:1572` from `iter_lines(decode_unicode=True)` to `iter_lines()` + manual `raw_line.decode("utf-8")`. However, `tools/think.py:91` has the exact same pattern and was missed. The `requests` library defaults to ISO-8859-1 decoding for `text/*` Content-Types without an explicit charset (RFC 2616 §3.7.1). `llama.cpp` returns `Content-Type: text/event-stream` without `charset=utf-8`, so any multi-byte UTF-8 in the think tool's response stream (emoji, CJK, math symbols) is corrupted before reaching `_THINK_RE.search()` or `_output()`.

Issue link: https://github.com/mblakemore/agent/issues/83
Related: issue #70 (special character display), cycle 0037.
Probe log: /tmp/agent-cicd/probes/0040-probe-baseline.log

## Success metric

- Baseline: `grep -c 'iter_lines(decode_unicode=True)' tools/think.py` = 1
- Target: 0
- Measurement method: `grep -c 'iter_lines(decode_unicode=True)' /tmp/agent-cicd/0040-think-iter-utf8/tools/think.py`

## Scope

- In: `tools/think.py` (line 91), `tests/test_stream_iter_utf8.py` (extend to cover think.py)
- Out: `agent.py` (already fixed in 0037), `tools/web_fetch.py` (does not use SSE streaming)

## Implementation steps

1. In `tools/think.py` line 91: change `for line in response.iter_lines(decode_unicode=True):` to use raw bytes + explicit UTF-8 decode, matching the pattern in `agent.py:1579`:
   ```python
   for raw_line in response.iter_lines():
       line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
   ```
2. In `tests/test_stream_iter_utf8.py`: add two new test methods:
   - `test_think_no_decode_unicode` — static: `iter_lines(decode_unicode=True)` must not appear in `tools/think.py`
   - `test_think_has_utf8_decode` — static: manual UTF-8 decode pattern must be present in `tools/think.py`
3. Run full test suite to confirm green.
4. Commit with message `CICD 0040 (#83): fix think.py iter_lines UTF-8 decode`.

## Test plan

- Existing tests that must stay green: all 188 currently passing
- New tests to add: 2 static guard tests in `tests/test_stream_iter_utf8.py`
  - `test_think_no_decode_unicode`: asserts `iter_lines(decode_unicode=True)` count in `tools/think.py` == 0
  - `test_think_has_utf8_decode`: asserts `.decode("utf-8")` or `.decode('utf-8')` appears in `tools/think.py`
- Re-run probe: static grep confirms count == 0

## Risks & mitigations

- Risk: `response.iter_lines()` returns bytes when the server sends bytes, but could return str if `requests` somehow resolves charset — `isinstance(raw_line, bytes)` guard handles both cases
- Risk: Other code in `think.py` loop expects `line` to be a str — it will be: `raw_line.decode("utf-8")` returns str; the `isinstance` fallback also returns str

## Rollback

`git revert HEAD` inside the worktree, or simply don't merge the PR. The parent checkout is untouched.

## Closes

Closes #83
