# 0007 — search-files-path-in-header

**Issue**: #13 — friction: search_files 0-results message doesn't name the searched directory, so agents can't tell they searched the wrong place
**Branch**: `cicd/0007-search-files-path-in-header`

## Goal

Make `search_files` name the absolute directory it actually walked, and surface a one-line hint when zero files were searched, so an agent that accidentally ran it against an empty cwd gets unambiguous feedback on turn 1 instead of burning two more search attempts and a shell fallback.

## Motivation

Cycle 0007 P-enum baseline (log: `/tmp/agent-cicd/probes/0007-enum-before.log`) — same prompt as cycle 0003's probe, but run from `/tmp/probe-0007-enum` instead of inside the repo. Result: **4 tool calls vs cycle 0003's target of 2**, because the first two `search_files` calls returned:

```
[Searched 0 files, 0 matched, 0 results]
No matches found.
```

Nothing in that output told the model *which* directory it walked, so it couldn't tell "my pattern is wrong" from "my cwd is wrong". It tried the same search again with a tiny variation, fell back to `exec_command find …`, then finally passed `path=/mnt/droid/repos/agent` on call 4 and got real results.

The ambiguity is the bug. Naming the searched directory in the header — especially in the 0-file case — turns "???" into "oh, I searched /tmp/probe-0007-enum instead of the repo" on turn 1.

Cycle 0003 learned (its results file, last bullet): *"Optional parameters don't move metrics unless the model reaches for them. Changing the default was what moved the number."* Same lesson applies here — making the feedback **part of every response shape**, not an opt-in `verbose=true`, is what makes the fix land.

## Success metric

**Primary (robust, code-level)**:

- Baseline: `tests/test_search_files.py` currently has **0 tests** that pin the searched-directory identity in the header, and **0 tests** that pin the 0-files hint body.
- Target: **add ≥ 3 new tests** covering (a) header names the resolved absolute path on a successful search, (b) header on a path-exists-but-no-match search still omits the hint, (c) 0-files path-default case emits the hint line; and the full suite stays **110/110 → ≥ 113/113** passing.
- Measurement:
  ```bash
  cd /tmp/agent-cicd/0007-search-files-path-in-header
  python3 -m unittest discover tests 2>&1 | tail -3
  ```

**Secondary (probe-level, nice-to-have signal)**:

- Baseline: P-enum from `/tmp/probe-0007-enum` — **4 tool calls** (`/tmp/agent-cicd/probes/0007-enum-before.log`).
- Target: **≤ 3 tool calls** on re-run from a clean `/tmp/probe-0007-enum`. I'm deliberately not chasing ≤ 2 because model behavior is noisy and the robust win is the unambiguous feedback, not a specific count.
- Measurement:
  ```bash
  grep -c '^INFO: TOOL CALL:' /tmp/agent-cicd/probes/0007-enum-after.log
  ```

**Done-when**: both metrics met and the full test suite is green.

## Scope

- **In**:
  - `tools/search_files.py` — header construction and zero-match return body.
  - `tests/test_search_files.py` — new test class for the header identity and zero-files hint.
- **Out**:
  - Changing the `path` default (stays `"."`).
  - Auto-promoting to git root, or any magical cwd detection.
  - Touching the context/grouping code paths already covered by cycle 0003's tests.
  - Any change to the display-compaction layer in `callbacks.py`.

## Implementation steps

1. In `tools/search_files.py`, resolve the search path once: after the `search_path.exists()` guard, compute `resolved = search_path.resolve()`. If `resolve()` raises (unlikely, since we've just proved `exists()`), fall back to `search_path.absolute()`.
2. Rewrite the header construction (~line 116) from:
   ```python
   header = f"[Searched {files_searched} files, {files_matched} matched, {total_matches} results"
   ```
   to:
   ```python
   header = (
       f"[Searched '{resolved}' "
       f"({files_searched} files, {files_matched} matched, {total_matches} results)"
   )
   ```
   so the terminating `)]\n` preserves the existing `_body()` test helper contract (partition on `]\n`).
3. Keep the `truncated` branch inside the parens so the closing `]\n` remains the partition point.
4. In the zero-match return (`if total_matches == 0`), split the body by whether any files were even walked:
   ```python
   if total_matches == 0:
       if files_searched == 0:
           return (
               header
               + f"No files were searched under '{resolved}'. "
               f"If you meant a different directory, pass path= with an absolute path."
           )
       return header + "No matches found."
   ```
5. Do **not** touch the non-zero-match return paths — the body shape is pinned by cycle 0003's tests and must stay byte-identical.
6. Add a new `TestSearchFilesHeaderIdentity` test class in `tests/test_search_files.py` with at least these tests:
   - `test_header_names_resolved_absolute_path_on_hit` — one file, one match, assert the header contains `f"'{Path(d).resolve()}'"`.
   - `test_header_names_resolved_absolute_path_on_miss_with_files` — one file, no match, assert the header names the directory and the body is the unchanged `"No matches found."` (no hint line).
   - `test_zero_files_emits_hint_line` — empty temp dir, assert the body contains the hint substring and names the resolved absolute path.
   - `test_header_shape_body_partition_still_works` — call `_body()` helper on a hit result, assert the partition still strips the header cleanly (guards against accidentally breaking cycle 0003's test helper contract).
7. Run the full suite in the worktree. If any cycle 0003 test fails, the header shape change broke the partition contract — revisit step 2 before touching anything else.
8. Run the probe from a fresh `/tmp/probe-0007-enum` against the worktree's `agent.py`, save to `/tmp/agent-cicd/probes/0007-enum-after.log`, count tool calls, record the delta.

## Test plan

- **Existing tests that must stay green**: all 110 in `tests/`, especially the cycle 0003 suite in `tests/test_search_files.py` (the `_body()` helper partitions on `]\n`, so the new header must still end with `)]\n`).
- **New tests I'll add** (all in `tests/test_search_files.py`):
  - `test_header_names_resolved_absolute_path_on_hit` — primary fix: directory identity present when results exist.
  - `test_header_names_resolved_absolute_path_on_miss_with_files` — path exists, pattern doesn't match, hint line must NOT appear.
  - `test_zero_files_emits_hint_line` — covers the exact P-enum probe symptom.
  - `test_header_shape_body_partition_still_works` — tripwire for the cycle 0003 test helper contract.
- **Re-run probe**: P-enum from `/tmp/probe-0007-enum`. Expected delta: 4 → ≤ 3 tool calls. Qualitative signal: the first `search_files` call's result should visibly name the wrong directory, cueing the model to pass `path=` on call 2.

## Risks & mitigations

- **Header shape breaks cycle 0003's `_body()` helper.** → Keep `]\n` as terminator and add `test_header_shape_body_partition_still_works` as an explicit tripwire.
- **Resolved absolute path is a very long string and crowds the header.** → Acceptable. The header is one line and is exactly the identifier the model needs; compaction in the display layer is separate.
- **`Path.resolve()` raises on a nonexistent path.** → Already guarded by `search_path.exists()`. Fall back to `search_path.absolute()` on any exception.
- **Probe metric is noisy.** → Primary metric is unit tests, deterministic. Probe is secondary evidence. If the probe comes back at 4 tool calls but the first result visibly names the wrong directory, I'll still null-result it because model behavior didn't change.
- **Model ignores the hint.** → Same as cycle 0003's lesson. Making the signal *unmissable* is the right lever; I can't force the model to read it.

## Rollback

Single file in source (`tools/search_files.py`) and one file in tests (`tests/test_search_files.py`). Worktree branch `cicd/0007-search-files-path-in-header` stays isolated from main; if verification fails irrecoverably, follow the Phase 8 null-result path (delete branch, comment on #13, leave issue open).

## Closes

Closes #13
