# 0038 — count-only-truncation

**Issue**: #79 — bug: search_files count_only=True still truncates at _MAX_RESULTS=100 — returns wrong count
**Branch**: cicd/0038-count-only-truncation (will be created in Phase 6)

## Goal

Fix `search_files(count_only=True)` so it returns the true total match count instead of capping at `_MAX_RESULTS=100`.

## Motivation

Cycle 0036 (PR #77) added a `count_only` parameter to `tools/search_files.py` with the intent of returning an accurate match count. However, the early-termination guard that prevents the tool from returning more than 100 result *lines* was not disabled for the `count_only` path. When there are more than 100 matches, `count_only=True` still reports `100 results (truncated)` — an undercount.

Probe P-count (cycle 0038): `search_files(count_only=True, pattern='def test_', path='/mnt/droid/repos/agent/tests/')` returned `100 results (truncated)`. Real count: 182. The agent reported "100 test functions" to the user — a 45% undercount.

Probe log: `/tmp/agent-cicd/probes/0038-P-count-before.log`

## Success metric

- Baseline: `search_files(count_only=True, pattern='def test_', path='/mnt/droid/repos/agent/tests/')` returns `100 results (truncated)` — wrong
- Target: same call returns `182 results` (no truncation marker)
- Measurement method:
  ```python
  python3 -c "
  import sys; sys.path.insert(0, '/tmp/agent-cicd/0038-count-only-truncation')
  from tools.search_files import fn
  result = fn('def test_', path='/mnt/droid/repos/agent/tests/', count_only=True)
  print(result)
  assert '182' in result, f'Expected 182 but got: {result}'
  assert 'truncated' not in result, f'Should not be truncated: {result}'
  print('PASS')
  "
  ```

## Scope

- In: `tools/search_files.py` — guard the 3 early-exit sites with `if not count_only`
- Out: display mode (non-count_only) — the `_MAX_RESULTS=100` cap stays for regular searches
- Out: the `header` format — unchanged except removing the `(truncated)` suffix when not truncated

## Implementation steps

1. In `tools/search_files.py`, in the per-line loop (line 80-81), add `count_only` guard:
   ```python
   if not count_only and total_matches + len(hit_nums) >= _MAX_RESULTS:
       break
   ```

2. In the `context == 0` branch (lines 92-94), guard the truncation break:
   ```python
   total_matches += len(hit_nums)
   if not count_only and total_matches >= _MAX_RESULTS:
       truncated = True
       break
   continue
   ```

3. In the `context > 0` branch (lines 120-123), guard the outer truncation break:
   ```python
   total_matches += len(hit_nums)
   if not count_only and total_matches >= _MAX_RESULTS:
       truncated = True
       break
   ```

4. Add regression tests in `tests/test_search_files_count_only.py`:
   - `test_count_only_above_max_results` — seed >100 matching files/lines, assert `count_only=True` returns the real count untruncated
   - `test_count_only_not_truncated_label` — same case, assert `(truncated)` is absent from the header
   - `test_count_only_display_still_truncates` — with `count_only=False`, confirm the 100-result cap still applies

## Test plan

- Existing tests: all 182 must stay green (`tests/test_search_files_count_only.py` has 4 tests covering the feature)
- New tests in `tests/test_search_files_count_only.py`:
  - `test_count_only_above_max_results` — creates a temp dir with 110 files each containing a match line, calls `fn(count_only=True)`, asserts result contains "110" and not "truncated"
  - `test_count_only_not_truncated_label` — same scenario, explicit `(truncated)` absence check
  - `test_display_mode_still_truncates` — same setup, `count_only=False`, assert "100 results" and "(truncated)" present

## Risks & mitigations

- **Risk**: Removing the cap for `count_only` on a huge codebase could be slow (scanning millions of files).
  → **Mitigation**: The cap was 100 *result lines*, not 100 files. Even without the cap, `count_only` never builds `match_lines` or `context_groups` — it only increments a counter. Scanning is O(n lines) not O(n result lines). The performance path is identical to a normal search; we just don't stop early.
- **Risk**: A test that was asserting `(truncated)` in the header for `count_only` mode might break.
  → **Mitigation**: Check `tests/test_search_files_count_only.py` — it doesn't assert truncation behavior currently. All existing 4 tests will remain valid.

## Rollback

`git revert <commit>` — the change is 3 one-line guards in `search_files.py`. Revert restores the capped (but incorrect) behavior.

## Closes

Closes #79
