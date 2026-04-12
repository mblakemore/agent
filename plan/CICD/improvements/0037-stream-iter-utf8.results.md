# 0037 — stream-iter-utf8 — results

- Issue: #70
- Branch: cicd/0037-stream-iter-utf8
- PR: (pending)
- Commit range: 2404fc3..2404fc3
- Date: 2026-04-12

## Metric

- Baseline: `grep -c 'iter_lines(decode_unicode=True)' agent.py` = 1
- After:    0
- Delta:    -1 (-100%) — Latin-1 decode path eliminated

Secondary metrics:
- Test count: 178 → 182 (+4 new tests)
- Manual UTF-8 decode: `raw_line.decode("utf-8")` present at agent.py:1573

## Test suite

- Before: 178 passing
- After:  182 passing (+4 new tests in test_stream_iter_utf8.py)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0037-tests-after.log
- Verdict: PASS (all 182 tests green, metric gates satisfied)
- Behavioral verification: emoji bytes decoded as UTF-8 round-trip correctly
  (star 🌟 sparkle ✨ party 🎉 — confirmed via Python simulation)

## What I actually changed

- `agent.py:1572`: Changed `for line in response.iter_lines(decode_unicode=True):`
  to `for raw_line in response.iter_lines():` + `line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line`
- `tests/test_stream_iter_utf8.py`: 4 new tests:
  - `test_iter_lines_no_decode_unicode` — static: old pattern absent from agent.py
  - `test_iter_lines_has_utf8_decode` — static: new decode pattern present
  - `test_utf8_bytes_round_trip` — behavioral: emoji bytes round-trip via UTF-8
  - `test_latin1_decode_corrupts_emoji` — documents the bug: Latin-1 decode corrupts same bytes
- `plan/CICD/improvements/0037-stream-iter-utf8.md`: plan file

## What I learned

- Cycle 0034 (`stream-unicode-passthrough`) fixed the wrong layer — it removed `_sanitize_display`
  from `_ReasoningRenderer`, which guards the Python layer, but the corruption happened before the
  renderer: in the HTTP response decoding. Issue #70 was still open because the root cause was upstream.
- `requests` defaults to ISO-8859-1 for `text/*` Content-Types without an explicit charset (per
  RFC 2616 §3.7.1). `llama.cpp` returns `Content-Type: text/event-stream` without `charset=utf-8`.
  Using `iter_lines(decode_unicode=True)` silently corrupts all multi-byte UTF-8 in SSE streams.
- The fix is minimal (2-line change) but impactful — it affects every non-ASCII character the model
  outputs: emojis, CJK, Arabic, math symbols, smart quotes from models running under llama.cpp.
- Pattern to remember: always decode SSE/SSE-like streams explicitly as UTF-8 rather than relying
  on Content-Type charset inference. LLM server implementations frequently omit charset from
  `text/event-stream` headers.
