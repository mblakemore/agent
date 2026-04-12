# 0027 — file-blocked-write-append-delete

**Issue**: #58 — bug: file tool write/append/delete actions lack _BLOCKED_FILENAMES guard
**Branch**: cicd/0027-file-blocked-write-append-delete (will be created in Phase 6)

## Goal

Extend the `_BLOCKED_FILENAMES` protection in `tools/file.py` from `_read` only to `_write`, `_append`, and `_delete` so no file action can corrupt or destroy internal runtime files like `conversation_checkpoint.json`.

## Motivation

`_read` checks `if p.name in _BLOCKED_FILENAMES` and returns an error. `_write`, `_append`, and `_delete` have no such check, leaving three unguarded paths:
- `_write` to a non-existent blocked filename: no read-first gate, no blocked check → creates corrupt checkpoint
- `_append` to an existing blocked filename: no read-first gate, no blocked check → appends junk
- `_delete` on a blocked filename: no blocked check → removes checkpoint, losing resumability

Issue #58. Probe log: /tmp/agent-cicd/probes/0027-pcount-before.log.

## Success metric

- Baseline: `grep -c 'if p.name in _BLOCKED_FILENAMES' tools/file.py` = 1 (read only)
- Target:  4 (read + write + append + delete)
- Measurement method: `grep -c 'if p.name in _BLOCKED_FILENAMES' /path/to/worktree/tools/file.py`
- Secondary: `test_file_tool.py` test count: 2 → 5 (+3 new regression tests)

## Scope

- In: `tools/file.py` (add 3 blocked-filename guards), `tests/test_file_tool.py` (add 3 regression tests)
- Out: `_insert` (already effectively guarded by read-first gate), any other file, README

## Implementation steps

1. In `tools/file.py::_write`: add `if p.name in _BLOCKED_FILENAMES: return Error ...` as the first check (before the read-first gate)
2. In `tools/file.py::_append`: add `if p.name in _BLOCKED_FILENAMES: return Error ...` as the first check
3. In `tools/file.py::_delete`: add `if p.name in _BLOCKED_FILENAMES: return Error ...` as the first check
4. In `tests/test_file_tool.py`: add class `TestBlockedFilenames` with three tests:
   - `test_write_blocked_filename_returns_error`: call `fn('write', 'conversation_checkpoint.json', 'x')`, assert result starts with "Error:"
   - `test_append_blocked_filename_returns_error`: create `conversation_checkpoint.json`, call `fn('append', ...)`, assert result starts with "Error:"
   - `test_delete_blocked_filename_returns_error`: create `conversation_checkpoint.json`, call `fn('delete', ...)`, assert result starts with "Error:" and file still exists

## Test plan

- Existing tests that must stay green: `tests/test_file_tool.py` (2 tests), all 144 in suite
- New tests: `tests/test_file_tool.py::TestBlockedFilenames` — 3 tests covering write/append/delete blocked paths
- Re-run probe: P-count (same as before) — expected no change (tool behavior for non-blocked files unchanged)

## Risks & mitigations

- Risk: error message wording differs from `_read` → mitigation: use parallel wording `"'%s' is an internal runtime file and cannot be written."` etc.
- Risk: changing `_write` could break existing `test_write_creates_missing_parent_dirs` → mitigation: test uses a non-blocked filename, unaffected
- Risk: `_append` guard check must use `Path(path).name` not `path` directly → already using `p.name` pattern from `_read`, follow same idiom

## Rollback

Delete the worktree branch. The parent `main` checkout is untouched.

## Closes

Closes #58
