# 0043 — file-list-orient-hint

**Issue**: #21 — friction: file tool list action has no 'skip me if you already know the path' hint, so agents waste a turn orienting
**Branch**: cicd/0043-file-list-orient-hint (will be created in Phase 6)

## Goal

Add an `IMPORTANT:` clause to the `file` tool's `list` action description that tells the agent to
skip the `list` step when the user's prompt already names the files involved.

## Motivation

Probe P-bug (cycle 0043, `/tmp/agent-cicd/probes/0043-pbug-before.log`) recorded 8 tool calls on a
task where the user named both files (`running_max.py`, `test_running_max.py`). The very first call
was `file(action='list', path='.')` — pure orientation overhead with zero information gain, because
all relevant paths were already in the prompt. Issue #21 (filed by cycle 0011) documents this reflex
and proposes exactly this fix: a one-sentence nudge in the `list` action description.

Probe log path: `/tmp/agent-cicd/probes/0043-pbug-before.log`

## Success metric

- **Baseline**: tool-call count on P-bug probe = **8** (cycle 0043 run,
  `/tmp/agent-cicd/probes/0043-pbug-before.log`)
- **Target**: ≤ **7** tool calls (remove the wasted orientation `file list`)
- **Measurement method**:
  ```bash
  grep -cE '^\s+->' /tmp/agent-cicd/probes/0043-pbug-after.log
  ```
  Must print 7 or less.

Secondary metric: `grep -c 'skip.*list\|already know' tools/file.py` must be ≥ 1 after the change
(confirms the hint text landed).

## Scope

- **In**: `tools/file.py` — update the `list` action bullet in the `definition` description string
- **In**: `tests/test_file_tool_definition.py` (new file) — static assertion that the `list`
  description contains the guidance substring
- **Out**: no change to the `fn()` function logic, `_list()` implementation, or any other file

## Implementation steps

1. In `tools/file.py`, replace the `list` description line (currently line 257):
   ```python
   "- list: List directory contents.\n"
   ```
   with:
   ```python
   "- list: List directory contents. "
   "IMPORTANT: skip this action if the user's prompt already names the files or paths you need — "
   "calling list when you already know the paths wastes a turn.\n"
   ```

2. Create `tests/test_file_tool_definition.py` with one test class `TestFileToolListHint`:
   - `test_list_description_contains_skip_hint` — loads `tools/file.py` as text, asserts that
     the `list` action bullet contains the phrase `"skip this action"`, confirming the hint landed
     and will survive future edits.

3. Run full test suite to confirm 193 existing tests stay green.

4. Re-run P-bug probe against the worktree and count tool calls.

## Test plan

- Existing tests that must stay green: all 193 (run with `python3 -m unittest discover tests`)
- New tests: `tests/test_file_tool_definition.py`
  - `test_list_description_contains_skip_hint` — static substring check on `tools/file.py`
- Re-run probe: P-bug (same seed — running_max.py with tests already passing);
  measure with `grep -cE '^\s+->' /tmp/agent-cicd/probes/0043-pbug-after.log`

## Risks & mitigations

- **Risk**: The LLM ignores the hint and still issues the `file list` — model behavior is
  non-deterministic.
  **Mitigation**: A single hint is still the right fix: prior cycles (0004, 0009) proved one
  `IMPORTANT:` clause is reliably respected by the Gemma 4 31B model. If the after-probe still
  shows the `file list`, record that as a finding and note it in the results, but do not abort:
  the static metric (hint text present) is guaranteed. The tool-call metric is the stretch goal.
- **Risk**: A future cycle edits the description and drops the hint.
  **Mitigation**: The new test catches regressions.
- **Risk**: The description string becomes harder to read with the addition.
  **Mitigation**: The addition is on the same bullet line, follows the existing pattern, and is
  shorter than the `write` bullet's guidance text.

## Rollback

`git revert <commit>` on the single commit inside the worktree. Parent checkout untouched.

## Closes

Closes #21
