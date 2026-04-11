# 0011 — file-list-skip-nudge

**Issue**: #21 — friction: file tool list action has no 'skip me if you already know the path' hint, so agents waste a turn orienting
**Branch**: cicd/0011-file-list-skip-nudge (will be created in Phase 6)

## Goal

Add a one-sentence nudge to the `list` action bullet in the `file` tool description so the model stops reflexively running `file(action='list', path='.')` on prompts where the user already named every target file, and lock the nudge in with a grep-based regression test on the tool definition.

## Motivation

Cycle 0011 PROBE ran P-bug against live `agent.py` in an empty temp dir after seeding a 15-line `running_max.py` and a 3-test `test_running_max.py` (one failing on all-negative input). Probe prompt named both files explicitly. The agent's turn 1 was `file(action='list', path='.')` — a pure orientation step for zero information gain, since both file paths were already in the prompt.

- Issue: #21 (full writeup)
- Probe log (before): `/tmp/agent-cicd/probes/0011-pbug-before.log` (6 turns, 5 tool calls, PASS)

The winning sequence would have been read → fix → re-run (3 tool calls after the initial test to see the failure, so 4 total). The `list` reflex turns that into 5. One tool call, one turn, one `ls` payload back through history — all saved by a one-sentence description edit.

Prior art for "add an IMPORTANT clause to a tool description to change model default":
- Cycle 0004 (file write auto-mkdir advertise): baseline 4 → after 3 tool calls on P-impl (−1, −25%).
- Cycle 0009 (search_files path warning): baseline 2 → after 1 tool call on P-enum (−1, −50%).

Same model (gemma-4-31B via llama-server), same shape of fix, same shape of regression guard. The pattern is stable on this stack.

## Success metric

- **Metric**: tool-call count on the P-bug probe from the seeded `/tmp/probe-0011b/` scaffold.
- **Baseline**: **5** (`/tmp/agent-cicd/probes/0011-pbug-before.log` — turn 1 `file list`, turn 2 `exec_command` test run, turn 3 `file read`, turn 4 `file write` fix, turn 5 `exec_command` re-run)
- **Target**: **≤ 4** (the `file list` turn drops out; the remaining 4 are the irreducible read → fix → re-run plus the first test-run that reveals the failure)
- **Measurement method**:

  ```bash
  grep -c '^  -> ' /tmp/agent-cicd/probes/0011-pbug-after.log
  ```

  The `'^  -> '` prefix is how `agent.py`'s console renderer marks each executed tool call (one line per call, even in the new deduped console from cycle 0008). Cross-checked against the before log: `grep -c '^  -> ' /tmp/agent-cicd/probes/0011-pbug-before.log` prints `5`. A successful cycle prints `4` or less.

## Scope

- **In**:
  - `tools/file.py` — edit the `definition["function"]["description"]` string: replace the `"- list: List directory contents."` bullet with an active sentence that both describes what the action does and explicitly tells the model when to skip it.
  - `tests/test_file_tool.py` — add a new `TestCase` class `TestFileListDescriptionDiscouragesWastedOrient` with one test asserting the new guidance substring is present in the `file` tool description.
- **Out**:
  - No behavioral change to `_list()` or any other `file` action. This is a description-only edit.
  - No change to the `path` parameter description for the `list` action (the current `"For 'list', defaults to current directory."` is still accurate).
  - No edits to any other tool's description. If the same "orient first" reflex also appears via `exec_command('ls ...')`, that's a separate cycle — I won't expand scope here.
  - No system-prompt edit. Prior art (cycles 0004, 0009) shows tool-description nudges are enough on this model; a system-prompt edit would be a heavier hammer and a wider blast radius.

## Implementation steps

1. **Edit `tools/file.py` — `list` bullet**. Change the current line
   `"- list: List directory contents.\n"`
   to an active, instruction-carrying sentence, e.g.
   `"- list: List directory contents. Only use this to discover files you don't already know about — if the user's prompt already named the files you need to touch, read them directly instead of listing first.\n"`
   The exact wording may shift slightly during the gap-fill pass; the two load-bearing substrings the regression test will assert on are `"already named"` and `"read them directly"`.
2. **Add `TestFileListDescriptionDiscouragesWastedOrient` to `tests/test_file_tool.py`**. One test: read `file_tool.definition["function"]["description"]`, lowercase it, assert both `"already named"` and `"read them directly"` are substrings. Failure message names the offending description and the missing phrase.
3. **Re-read the edited description** back in place (no trailing/leading whitespace weirdness, the markdown-ish bullet structure is unchanged, and the `IMPORTANT:` trailing clause about reading-before-writing is still present and still the last sentence).
4. **Run the full test suite** — expect 119 → 120 passing. No existing test asserts on the exact `list` bullet text, so the edit is safe.
5. **Re-run the P-bug probe** against the worktree's `agent.py` from a fresh empty tempdir with a fresh seed of `running_max.py` + `test_running_max.py` (same content as `/tmp/probe-0011b/`). Save to `/tmp/agent-cicd/probes/0011-pbug-after.log` and compute the metric.
6. **Iterate if needed**. If the after run still shows 5 tool calls, the nudge wording wasn't strong enough — try a stronger phrasing with the word `skip` explicit (budget: 3 debug iterations per Hard Rule #5, then null-result).
7. **Write `results.md`, append progress row, commit plan + results + progress**. One commit per logical step: (a) description edit, (b) regression test, (c) plan + results + progress row.

## Test plan

- **Existing tests that must stay green**: all 119 tests under `tests/`, and specifically:
  - `tests/test_file_tool.py` (currently 2 tests including the auto-mkdir description assertion — the edit I'm making adds a new bullet without touching the `write` bullet, so the existing assertion `"Parent directories are created automatically"` must still match).
  - `tests/test_doc_sync.py` (new in cycle 0010 — doesn't touch `tools/file.py` but uses the same grep pattern I'm replicating).
  - `tests/test_search_files.py::TestSearchFilesDefinition::test_definition_warns_about_cwd_default` (cycle 0009 — proves the description-assertion idiom is stable).
- **New tests I'll add**:
  - `tests/test_file_tool.py::TestFileListDescriptionDiscouragesWastedOrient::test_list_description_discourages_wasted_orient` — grep-based, asserts both `"already named"` and `"read them directly"` (case-insensitive) are substrings of the `file` tool description. Two substrings (not one) so a future reword can relax either half without a silent drift.
- **Re-run probe**: **P-bug from `/tmp/probe-0011c/`** (fresh empty dir, identical seed to `/tmp/probe-0011b/` but a different name so the before log stays untouched). Expected metric: tool-call count drops 5 → ≤4. Log saved to `/tmp/agent-cicd/probes/0011-pbug-after.log`.

## Risks & mitigations

- **Risk**: the model ignores the nudge (the description is already long-ish and the model might skim past a new bullet clause).
  - **Mitigation**: position the new clause at the end of the bullet, right before the following bullet, where the token is freshest. Prior art: cycles 0004 and 0009 both placed their nudges at the end of their respective bullets and both landed.
- **Risk**: the new nudge bleeds into other use cases — e.g. the model stops using `list` even when it legitimately needs to discover files (exploring an unfamiliar subdir).
  - **Mitigation**: the phrasing says "if the user's prompt already named the files you need to touch" — the conditional is explicit, so prompts that don't name files keep the old behavior. The probe is a named-files scenario, but if after the fix the P-impl probe (which doesn't need `list` at all) regresses, that's a signal the nudge overshot. I'll re-run P-impl as a regression check in Phase 7.
- **Risk**: the regression test phrases become load-bearing strings that block future wording tweaks.
  - **Mitigation**: two independent substrings (`"already named"`, `"read them directly"`) each of which is short and natural English. A future reword that keeps either concept can keep the test green; a reword that drops both has almost certainly dropped the nudge, which is the intended fail mode.
- **Risk**: P-bug is probe-specific — a different bug scenario might not reproduce the orient reflex.
  - **Mitigation**: cycles 0004/0009 measured on single-scenario probes too, and the wins stayed stable on subsequent cycles' probes (0007 re-ran P-enum, 0010 re-ran P-count, both matched prior after-numbers). Single-scenario probe is the CICD standard.

## Rollback

One `git revert` on the three commits of `cicd/0011-file-list-skip-nudge` restores the original description and deletes the new test class. No schema, no state, no dependencies, no behavioral change to `_list()`.

## Closes

Closes #21
