# 0036 — search-files-count-only — results

- Issue: #76
- Branch: cicd/0036-search-files-count-only
- PR: (pending)
- Commit range: 6466607..6466607
- Date: 2026-04-12

## Metric

- Baseline: `grep -c '"count_only"' tools/search_files.py` = 0
- After:    1
- Delta:    +1 (+100%) — new parameter added to tool definition

Secondary metrics:
- Test count: 174 → 178 (+4 new tests)
- `count_only` occurrences in search_files.py: 0 → 5 (sig, docstring, body, desc, properties)

## Test suite

- Before: 174 passing
- After:  178 passing (+4 new tests in test_search_files_count_only.py)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0036-tests-after.log
- Verdict: PASS (all 178 tests green, metric gates satisfied)

## What I actually changed

- `tools/search_files.py`:
  - Added `count_only: bool = False` parameter to `fn()` signature
  - Added docstring entry for `count_only`
  - Added short-circuit after header is built: `if count_only: return header.rstrip("\n")`
  - Updated tool `description` to advertise `count_only` for counting tasks
  - Added `"count_only"` property to `definition["function"]["parameters"]["properties"]`
- `tests/test_search_files_count_only.py`: 4 new tests covering behavioral (with/without matches), edge case (zero matches), regression guard (count_only=False still returns matches), and static schema check.

## What I learned

- The header line already contains all count information (`files_searched`, `files_matched`, `total_matches`) — adding `count_only` was purely a short-circuit before formatting, no re-computation needed.
- The short-circuit placement (after header build, before the `total_matches == 0` check) means `count_only=True` returns the header even when there are zero matches, giving a clean `0 results` response without the "No matches found." prose — cleaner for counting tasks.
- Advertising new parameters explicitly in the tool description ("Pass count_only=true when you only need a match count") is important for model uptake — models read the description to decide which parameters to use.
