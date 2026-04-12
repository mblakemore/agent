# 0045 — path-rw-encoding

**Issue**: #92 — bug: Path.read_text()/write_text() calls lack encoding='utf-8'
**Branch**: cicd/0045-path-rw-encoding (will be created in Phase 6)

## Goal

Add `encoding='utf-8'` to all `Path.read_text()` and `Path.write_text()` calls
in the codebase, matching the pattern established by cycles 0042 and 0044 for
`open()` calls.

## Motivation

Cycles 0042 (#87) and 0044 (#90) fixed all `open()` calls to use explicit
`encoding='utf-8'`. The same codebase uses `Path.read_text()`/`write_text()` in
four files — none with explicit encoding. On non-UTF-8 locales (Windows ANSI,
some Linux configurations), these calls silently corrupt multi-byte content or
raise `UnicodeDecodeError`. Issue: #92. Probe log:
`/tmp/agent-cicd/probes/0045-pcount-before.log`.

## Success metric

- Baseline: `grep -c "encoding='utf-8'" tools/task_tracker.py tools/exec_command.py tools/search_files.py` scoped to read_text/write_text lines = **0**
- Target: **6** (one per call site below)
- Measurement method:
  ```bash
  grep -n "read_text\|write_text" tools/task_tracker.py agent.py tools/exec_command.py tools/search_files.py \
    | grep "encoding='utf-8'"
  ```
  must list all 6 lines.

## Scope

- In:
  - `tools/task_tracker.py` (lines 16, 24)
  - `agent.py` (line 470)
  - `tools/exec_command.py` (lines 245, 249)
  - `tools/search_files.py` (line 71)
- Out: test files, `tools/__init__.py`, `read_pdf.py` (uses `fitz.open`, not Python builtins)

## Implementation steps

1. `tools/task_tracker.py:16` — `p.read_text()` → `p.read_text(encoding='utf-8', errors='replace')`
   (reads JSON task store; `errors='replace'` on read matches the open() pattern for read paths)
2. `tools/task_tracker.py:24` — `p.write_text(json.dumps(...))` → `p.write_text(json.dumps(...), encoding='utf-8')`
3. `agent.py:470` — `p.read_text()` → `p.read_text(encoding='utf-8', errors='replace')`
   (reads @-reference file content injected into context; garbled UTF-8 here produces wrong context)
4. `tools/exec_command.py:245` — `wt.read_text()` → `wt.read_text(encoding='utf-8', errors='replace')`
   (reads temp file of command output to strip heredoc markers)
5. `tools/exec_command.py:249` — `wt.write_text(cleaned)` → `wt.write_text(cleaned, encoding='utf-8')`
6. `tools/search_files.py:71` — `file_path.read_text(errors="ignore")` → `file_path.read_text(encoding='utf-8', errors='ignore')`
   (reading source files; `errors='ignore'` is correct here, just add explicit encoding)

## Test plan

- Existing tests that must stay green: all 205 tests
- New tests: `tests/test_path_rw_encoding.py` — 6 static assertions, one per call site.
  Pattern: read source file, assert the specific `read_text`/`write_text` call includes `encoding='utf-8'`.
  Mirror of `tests/test_open_encoding_agent.py` for the `Path` method variants.
- Re-run probe: P-count — expect same result (1 tool call, PASS), no behavioral change

## Risks & mitigations

- `errors='replace'` on `task_tracker.py` read path could silently mask a corrupt tasks.json — acceptable, same as the open() pattern in the rest of the codebase. A corrupt file without encoding will raise an exception that's already caught.
- `exec_command.py` temp file cleanup is inside a `try/except Exception: pass` block — adding encoding won't widen the failure surface.
- `search_files.py` already uses `errors='ignore'` — just adding `encoding='utf-8'` is safe.

## Rollback

Delete worktree and branch: `git worktree remove /tmp/agent-cicd/0045-path-rw-encoding --force && git branch -D cicd/0045-path-rw-encoding`.

## Closes

Closes #92
