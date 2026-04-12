# 0042 — file-open-encoding

**Issue**: #87 — bug: tools/file.py open() calls lack explicit encoding='utf-8' — UnicodeDecodeError on non-UTF-8 files, encoding fragile on non-UTF-8 locales
**Branch**: cicd/0042-file-open-encoding (will be created in Phase 6)

## Goal

Add explicit `encoding='utf-8'` to all 7 `open()` calls in `tools/file.py`, with `errors='replace'` on read-mode calls, so the file tool never raises `UnicodeDecodeError` and works correctly on any locale.

## Motivation

Every `open()` call in `tools/file.py` uses no explicit `encoding=`. Python defaults to `locale.getpreferredencoding()` which is not guaranteed to be UTF-8. On systems with `LANG=C`, `LANG=en_US.ISO8859-1`, or similar non-UTF-8 locales, reading files with non-ASCII bytes raises `UnicodeDecodeError`, which wraps as `Error (read): 'utf-8' codec can't decode byte ...`. The agent receives an opaque error it cannot usefully diagnose.

Static proof: `grep -c "encoding='utf-8'" tools/file.py` = 0 (baseline).

Cycles 0037 and 0040 already fixed the SSE stream decode (agent.py + think.py). This is the same class of bug in the file I/O layer.

Issue link: https://github.com/mblakemore/agent/issues/87

## Success metric

- Baseline: `grep -c "encoding='utf-8'" tools/file.py` = 0
- Target: 7 (one for each of the 7 `open()` calls)
- Measurement method: `grep -c "encoding='utf-8'" /tmp/agent-cicd/0042-file-open-encoding/tools/file.py`

## Scope

- In: `tools/file.py` (7 `open()` calls), `tests/test_file_tool.py` (add 2 regression tests)
- Out: `agent.py`, `tools/exec_command.py`, all other files

## Implementation steps

1. **tools/file.py line 73** (`_read`, 'r' mode):
   ```python
   with open(p, 'r', encoding='utf-8', errors='replace') as f:
   ```

2. **tools/file.py line 113** (`_write`, 'r' mode — reads existing file before replacing lines):
   ```python
   with open(p, 'r', encoding='utf-8', errors='replace') as f:
   ```

3. **tools/file.py line 137** (`_write`, 'w' mode — writes new content):
   ```python
   with open(p, 'w', encoding='utf-8') as f:
   ```

4. **tools/file.py line 160** (`_write`, 'w' mode — full file write):
   ```python
   with open(p, 'w', encoding='utf-8') as f:
   ```

5. **tools/file.py line 173** (`_append`, 'a' mode):
   ```python
   with open(p, 'a', encoding='utf-8') as f:
   ```

6. **tools/file.py line 192** (`_insert`, 'r' mode):
   ```python
   with open(p, 'r', encoding='utf-8', errors='replace') as f:
   ```

7. **tools/file.py line 205** (`_insert`, 'w' mode — writes back after insert):
   ```python
   with open(p, 'w', encoding='utf-8') as f:
   ```

8. **tests/test_file_tool.py**: Add a new test class `TestFileReadEncoding` with two tests:
   - `test_read_non_utf8_file_returns_content_not_error`: creates a file with `\xff` byte, reads it, asserts result does NOT start with `"Error"` and DOES contain the replacement char `\ufffd` (or `?` depending on replacement mode)
   - `test_read_utf8_file_works_correctly`: smoke test that valid UTF-8 with non-ASCII chars (e.g., `café`) reads back correctly

9. Run full test suite to confirm green.

10. Commit with message `CICD 0042 (#87): add encoding='utf-8' to all open() calls in tools/file.py`.

## Test plan

- Existing tests that must stay green: all 191 currently passing
- New tests to add: 2 in `tests/test_file_tool.py`:
  - `TestFileReadEncoding.test_read_non_utf8_file_returns_content_not_error`
  - `TestFileReadEncoding.test_read_utf8_file_works_correctly`
- Re-run metric: `grep -c "encoding='utf-8'" tools/file.py` must equal 7

## Risks & mitigations

- Risk: `errors='replace'` silently replaces bad bytes with U+FFFD — agent may read corrupted content for binary files. Mitigation: this is strictly better than a crash; the agent at least gets content it can describe.
- Risk: the 4 read-mode opens serve different functions (`_read`, `_write` pre-check, `_insert` pre-check) — must add `errors='replace'` to all 3 read-mode opens. The count-check above catches any miss.
- Risk: existing tests may rely on the current behavior for binary files — check `test_file_tool.py` first. None of the existing tests use non-UTF-8 bytes.

## Rollback

`git revert HEAD` inside the worktree, or simply don't merge the PR. The parent checkout is untouched.

## Closes

Closes #87
