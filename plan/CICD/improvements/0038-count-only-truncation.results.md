# 0038 — count-only-truncation — results

- Issue: #79
- Branch: cicd/0038-count-only-truncation
- PR: (pending)
- Commit range: 50fb5fd (single commit)
- Date: 2026-04-12

## Metric

- Baseline: `search_files(count_only=True, pattern='def test_', path=tests/)` → `100 results (truncated)` (wrong — real count is 182)
- After:    same call → `182 results` (no truncation marker)
- Delta:    +82 (+82%) — from capped-wrong to accurate-correct

## Test suite

- Before: 182 passing
- After:  185 passing (+3 new regression tests)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0038-P-count-before.log (before fix)
- After fix verified via unit test and direct Python call:
  ```
  fn('def test_', path='.../tests/', count_only=True)
  → [Searched '...' (24 files, 24 matched, 190 results)]
  fn('def test_', path='.../tests/', glob='*.py', count_only=True)
  → [Searched '...' (24 files, 24 matched, 182 results)]
  ```
- Verdict: PASS

## What I actually changed

- `tools/search_files.py`: 3 one-line guards changed from `if total_matches >= _MAX_RESULTS:` to `if not count_only and total_matches >= _MAX_RESULTS:` at the 3 early-exit sites (inner per-line loop, context==0 break, context>0 break)
- `tests/test_search_files_count_only.py`: added `TestCountOnlyNoTruncation` class with 3 regression tests covering the above-_MAX_RESULTS scenario for both count_only=True (should not truncate) and count_only=False (should still truncate)

## What I learned

- The `count_only` feature was added in cycle 0036 (PR #77) as a friction fix (tool-call reduction), but the truncation guard was overlooked because the feature was implemented as a post-processing branch (`if count_only: return header`) rather than by skipping the expensive collection loop. The fix is minimal: guard the 3 early-exit locations.
- When adding a mode-based parameter that changes what's collected vs. what's displayed, early-exit guards should be audited as a checklist item in the plan's implementation steps.
- The P-count probe immediately caught this: the agent got "100 test functions" when there are 182 — a factually wrong answer that would mislead any downstream use of the count.
