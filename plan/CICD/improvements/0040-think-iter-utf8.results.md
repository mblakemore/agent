# 0040 — think-iter-utf8 — results

- Issue: #83
- Branch: cicd/0040-think-iter-utf8
- PR: #84 (draft)
- Commit range: 1f29c65..1f29c65
- Date: 2026-04-12

## Metric

| | Before | After | Delta |
|--|--------|-------|-------|
| `grep -c 'iter_lines(decode_unicode=True)' tools/think.py` | 1 | 0 | −1 (−100%) |

## Test suite

- Before: 188 passing
- After:  190 passing (+2 new static guard tests)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0040-tests-after.log
- Verdict: PASS (all 190 tests green, metric gate satisfied)

## What I actually changed

- `tools/think.py:91`: Changed `for line in response.iter_lines(decode_unicode=True):` to `for raw_line in response.iter_lines():` + `line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line` — identical two-line pattern as the agent.py fix in cycle 0037
- `tests/test_stream_iter_utf8.py`: Added 2 new static guard tests (`test_think_no_decode_unicode`, `test_think_has_utf8_decode`) covering `tools/think.py`; updated module docstring to reference both cycles 0037 and 0040; added `THINK_PY` path constant
- `plan/CICD/improvements/0040-think-iter-utf8.md`: Plan file

## What I learned

- Cycle 0037's test only guarded `agent.py` — the guard was too narrow. When a bug fix applies to one file in a shared pattern, the guard test should check all files with that pattern.
- The `think` tool has its own SSE streaming loop that's architecturally similar to `agent.py`'s main loop. Both need the same UTF-8 decode treatment.
- Guard test coverage should match the scope of the fix: if fixing a class of bug across a codebase, tests should assert the class is gone from all affected files, not just one.
