# 0039 — think-raw-print — Results

**Date**: 2026-04-12
**Issue**: #81
**Branch**: cicd/0039-think-raw-print

## Metric

| | Before | After | Delta |
|--|--------|-------|-------|
| `grep -c 'print(' tools/think.py` (non-comment) | 4 | 0 | −4 (−100%) |

## Probe

- Static grep: `grep -c 'print(' tools/think.py` (non-comment lines) = 0 ✓
- Behavioral: `test_think_output_is_injectable` confirms `_output` can be
  replaced without builtins.print being called ✓

## Test results

- Before: 185 tests passing
- After: 188 tests passing (+3 new tests)
- All pre-existing tests green ✓

## Changes

- `tools/think.py`: Added `_output = print` module-level injectable; replaced
  4 raw `print()` calls with `_output()`.
- `agent.py`: Added injection block after `_cb` setup in `run_agent_single` to
  wire `tools.think._output = lambda text: _emit("on_stream_chunk", text)`.
- `tests/test_think_no_print.py`: 3 tests — static no-print assertion,
  injectable attribute check, behavioral injection test.
- `plan/CICD/improvements/0039-think-raw-print.md`: Plan file.

## Verdict

PASS
