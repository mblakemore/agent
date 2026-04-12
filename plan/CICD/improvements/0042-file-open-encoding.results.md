# 0042 — file-open-encoding — results

- Issue: #87
- Branch: cicd/0042-file-open-encoding
- PR: (see below)
- Commit range: 86ef782
- Date: 2026-04-12

## Metric

- Baseline: `grep -c "encoding='utf-8'" tools/file.py` = 0
- After:    7
- Delta:    +7 (+∞%)

## Test suite

- Before: 191 passing
- After:  193 passing (+2 new regression tests)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0042-tests-after.log
- Verdict: PASS (193/193 tests green)

## What I actually changed

- `tools/file.py`: Added `encoding='utf-8'` to all 7 `open()` calls
  - 3 read-mode calls (lines 73, 113, 192) got `encoding='utf-8', errors='replace'`
  - 3 write-mode calls (lines 137, 160, 205) got `encoding='utf-8'`
  - 1 append-mode call (line 173) got `encoding='utf-8'`
- `tests/test_file_tool.py`: Added `TestFileReadEncoding` class with 2 tests:
  - `test_read_non_utf8_file_returns_content_not_error` — proves `\xff` byte returns content with U+FFFD rather than an Error string
  - `test_read_utf8_file_with_non_ascii_works_correctly` — proves `café ⋆ résumé` round-trips correctly

## What I learned

- All 7 `open()` calls in `tools/file.py` had been using Python's locale-dependent default encoding since the file was first written. This is the same class of bug fixed in cycles 0037 (agent.py SSE stream) and 0040 (think.py SSE stream) — but for the file I/O layer rather than the HTTP streaming layer.
- The `errors='replace'` mode is the right choice for read-mode file tools: it lets the agent see the content (with replacement chars) rather than an opaque error. This is strictly better than crashing.
- Static `grep -c "encoding='utf-8'"` is a clean, fast metric for this class of fix.
