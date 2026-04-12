# 0036 — search-files-count-only

**Issue**: #76 — friction: search_files has no count-only mode, forcing a fallback exec_command for counting tasks
**Branch**: cicd/0036-search-files-count-only (will be created in Phase 6)

## Goal

Add a `count_only` parameter to `search_files` so agents can get match counts in a single tool call without falling back to `exec_command`.

## Motivation

CICD cycle 0036 probe P-count (`Count the number of def test_* methods in tests/`) showed the agent uses `exec_command(command='grep -r "def test_" tests/ | wc -l')` to get a count. While this works in 1 tool call, it introduces a shell dependency. In restricted environments without `exec_command`, the agent would need to call `search_files` and then process the match list itself (wasting tokens on unused content). A `count_only=True` parameter that returns just `"174 matches in 12 files"` is a clean, low-risk addition.

Probe log: `/tmp/agent-cicd/probes/before-p-count.log`

## Success metric

- Baseline: `grep -c 'count_only' /mnt/droid/repos/agent/tools/search_files.py` = 0
- Target: 1 (the parameter exists in the function signature and definition)
- Measurement method: `grep -c 'count_only' <worktree>/tools/search_files.py`

Secondary metric: new test count
- Baseline: 174 passing
- Target: 174 + 4 = 178 (4 new tests for count_only behavior)

## Scope

- In: `tools/search_files.py` (add `count_only` param to `fn()` and to `definition`)
- In: `tests/test_search_files_count_only.py` (new test file with 4+ tests)
- Out: `agent.py`, `callbacks.py`, other tools — no changes needed

## Implementation steps

1. In `tools/search_files.py`, add `count_only: bool = False` to the `fn()` signature after `context: int = 3`.

2. In the function body, after the match-counting loop completes, add a short-circuit before formatting match lines:
   ```python
   if count_only:
       return header.rstrip("\n")
   ```
   The existing `header` line already contains `files_searched`, `files_matched`, and `total_matches` — this is exactly the information needed for a count response.

3. Update the `definition` dict:
   - Add `count_only` to `"parameters"` → `"properties"`:
     ```python
     "count_only": {
         "type": "boolean",
         "description": (
             "Return only the match count summary (files searched, files matched, "
             "total matches) without the match lines themselves. Use this when you "
             "only need to know how many matches exist, not where they are. "
             "Default: false."
         ),
         "default": False,
     },
     ```
   - Also update the tool `description` to mention count_only as an option for counting tasks.

4. In `tests/test_search_files_count_only.py`, add:
   - `test_count_only_returns_header_only`: call `fn(pattern, path, count_only=True)` on a temp dir with known content; assert result contains match counts and does NOT contain the file path lines.
   - `test_count_only_zero_matches`: `count_only=True` with a pattern that matches nothing; assert "No matches found." or the header.
   - `test_count_only_false_returns_matches`: confirm default (`count_only=False`) still returns match lines.
   - `test_count_only_in_definition`: static — assert `"count_only"` in `definition["function"]["parameters"]["properties"]`.

## Test plan

- Existing tests that must stay green: all 174 currently passing
- New tests:
  - `tests/test_search_files_count_only.py`: 4 tests
    - `test_count_only_returns_header_only` — behavioral with temp dir
    - `test_count_only_zero_matches` — edge case
    - `test_count_only_false_returns_matches` — regression guard
    - `test_count_only_in_definition` — static schema check
- Re-run probe: P-count — with `count_only` available the agent may choose `search_files` over `exec_command`; primary metric is the static grep count (0→1), not probe behavioral change.

## Risks & mitigations

- **Risk**: `count_only=True` short-circuits before the truncation logic; if there are >100 matches, header would say 100 but actual count could be higher.
  **Mitigation**: The existing `_MAX_RESULTS` cap already applies during the match loop — `total_matches` reflects matches up to the cap, and `(truncated)` suffix in the header communicates this. No special handling needed.
- **Risk**: The description update might confuse agents into always using `count_only`.
  **Mitigation**: Description says "use this when you only need to know how many matches exist, not where they are" — preserves the intent of full-match mode.

## Rollback

Delete the worktree branch; the parent `main` checkout is untouched.

## Closes

Closes #76
