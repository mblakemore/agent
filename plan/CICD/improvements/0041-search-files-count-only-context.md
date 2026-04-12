# 0041 — search-files-count-only-context

**Issue**: #85 — bug: search_files builds context windows even when count_only=True — wasted work
**Branch**: cicd/0041-search-files-count-only-context (will be created in Phase 6)

## Goal

When `count_only=True` and `context > 0` (the default is 3), skip the context-window building
loop in `search_files.fn`. Currently the `context > 0` branch allocates and populates `windows`
and `context_groups` for every matched file, then discards them — because `count_only=True`
returns early with just the header string.

## Motivation

`tools/search_files.py` lines 97–123 build per-file context windows unconditionally. For every
matched file, this allocates O(hits) `windows` entries and O(hits × 2×context) formatted
strings in `context_groups`, none of which are ever returned when `count_only=True`. The
`context == 0` branch already has a correct early-out path (line 95 `continue`), but the
`context > 0` branch is missing the equivalent guard.

This was missed in cycle 0036 (which added `count_only`) and cycle 0038 (which fixed truncation).

Issue link: https://github.com/mblakemore/agent/issues/85
Related: cycle 0036 (added count_only), cycle 0038 (fixed truncation).

## Success metric

- Baseline: `grep -c 'if count_only:' tools/search_files.py` = 1
- Target: 2
- Measurement: `grep -c 'if count_only:' /tmp/agent-cicd/0041-search-files-count-only-context/tools/search_files.py`

## Scope

- In: `tools/search_files.py` (add guard in context > 0 block), `tests/test_search_files_count_only.py` (add regression test)
- Out: `agent.py`, `callbacks.py`, all other files

## Implementation steps

1. **tools/search_files.py**: In the `context > 0` block (after line 95's `continue`), add:
   ```python
   # context > 0: build merged windows — skip entirely when count_only
   if count_only:
       total_matches += len(hit_nums)
       continue
   ```
   This goes immediately after `# context > 0: build merged windows` comment (line 97).

2. **tests/test_search_files_count_only.py**: Add a new test class
   `TestCountOnlyContextBypass` with one test:
   - `test_count_only_skips_context_windows_default_context`: calls `fn(pattern=...,
     path=..., count_only=True)` with default `context` (= 3) and asserts:
     - Result contains the correct count header
     - Result does NOT contain match lines (confirming context_groups were not used)
     - Result does NOT contain `--` context separators

3. Run full test suite to confirm green.

4. Commit with message `CICD 0041 (#85): skip context window building in search_files when count_only=True`.

## Test plan

- Existing tests that must stay green: all 190 currently passing
- New tests to add: 1 behavioral guard test in `tests/test_search_files_count_only.py`
  - `test_count_only_skips_context_windows_default_context`: asserts header-only output
    when context=3 (default) and count_only=True
- Re-run metric: `grep -c 'if count_only:' tools/search_files.py` must equal 2

## Risks & mitigations

- Risk: the existing `test_count_only_returns_header_only` test uses default context=3
  already and would catch any regression — the fix must keep that test green.
- Risk: the `total_matches += len(hit_nums)` line in the guard must match the existing
  accumulation logic — confirmed: this is identical to line 120's `total_matches += len(hit_nums)`.

## Rollback

`git revert HEAD` inside the worktree, or simply don't merge the PR. The parent checkout is untouched.

## Closes

Closes #85
