# 0023 — summary-max-chars-direct-access — Results

**Cycle**: 0023
**Date**: 2026-04-11
**Issue**: #50
**Branch**: cicd/0023-summary-max-chars-direct-access

## Outcome: PASS

## Metric

| | Value |
|--|--|
| Probe | P-count + static grep |
| Metric | `.get("summary_max_chars", N)` call sites in `agent.py` |
| Baseline | 1 |
| After | 0 |
| Delta | −1 (−100%) |

## What changed

- `agent.py:134`: replaced `_config["context"].get("summary_max_chars", 1500)` with `_config["context"]["summary_max_chars"]`
  - Removes stale fallback value `1500` (wrong; the defined default in `_DEFAULT_CONFIG` has always been `3000`)
  - Aligns with the access pattern used by all other context keys (lines 131–133, 135, 1186–1187)
- `tests/test_default_config.py`: added `TestDefaultConfigContextKeys` with two new tests:
  - `test_summary_max_chars_in_default_config`: asserts the key is present in `_DEFAULT_CONFIG["context"]`
  - `test_summary_max_chars_default_value`: asserts the value is `3000`

## Test results

- Before: 134 tests, all passing
- After: 136 tests, all passing (+2 new regression guards)

## Verification

```bash
grep -c '\.get("summary_max_chars"' agent.py
# → 0  (was 1)
```

```
Ran 136 tests in 2.134s
OK
```
