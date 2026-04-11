# 0015 — tool-spinner-noninteractive

**Issue**: #34 — friction: tool-call spinner prefix duplicates tool-start line in non-interactive logs
**Branch**: cicd/0015-tool-spinner-noninteractive

## Goal

Stop `agent.py` from rendering every non-streaming tool call's start line twice in non-interactive logs. One clean `  -> tool(args)` line per call, not a prefix + full-form pair.

## Motivation

Cycle 0015 PROBE (P-count, `/tmp/agent-cicd/probes/0015-pcount-before.log`) shows the duplication on line 20:

```
  -> exec_command   -> exec_command(command='grep -r "def test_" /mnt/droid/repos/agent/tests/…)
```

The first `  -> exec_command ` is written synchronously by `StreamStatus.start()` in its non-interactive branch (`spinner.py:59-61`). The second `  -> exec_command(...)` is printed by `ConsoleCallback.on_tool_start` at `callbacks.py:280`, whose leading `{theme.CLEAR_LINE}` expands to `""` when `theme._no_color()` is true (`theme.py:48`). Nothing erases the first prefix, so both land in the log.

Cycle 0014 (`plan/CICD/improvements/0014-continue-none-dedup.md:46`) already flagged this as a follow-up. Filed as issue #34 in Phase 2 of this cycle.

The spinner's non-interactive prefix write is still required by the assistant-streaming header at `agent.py:1558-1559` (otherwise the streamed reply would have no `Assistant:` label in captured logs), so the fix belongs at the *caller* — gating `use_spinner` on interactivity — not inside `spinner.py`.

## Success metric

- **Baseline**: `grep -cE '^  -> \w+ +-> \w+\(' /tmp/agent-cicd/probes/0015-pcount-before.log` → **1** (one non-streaming tool call, one duplicated-prefix line).
- **Target**: same grep against `/tmp/agent-cicd/probes/0015-pcount-after.log` → **0** (no duplicated-prefix lines, no matter how many tool calls the after-run makes).
- **Sanity gate**: the after-run probe log must still contain at least one `^  -> \w+\(` line — i.e. the tool-start lines still render, they're just not duplicated. This rules out the false "0 because the run had 0 tool calls" outcome.
- **Measurement method**:
  ```bash
  grep -cE '^  -> \w+ +-> \w+\(' /tmp/agent-cicd/probes/0015-pcount-before.log
  grep -cE '^  -> \w+ +-> \w+\(' /tmp/agent-cicd/probes/0015-pcount-after.log
  grep -cE '^  -> \w+\(' /tmp/agent-cicd/probes/0015-pcount-after.log  # sanity: must be ≥ 1
  ```

## Scope

- **In**:
  - `agent.py:1785` — extend the `use_spinner` condition so the spinner is suppressed in non-interactive mode. `use_spinner = func_name not in _STREAMING_TOOLS and not theme._no_color()`. Requires `theme` already imported at module level — it is, indirectly via `from spinner import StreamStatus` and `import theme` usages in the file. If `theme` is not directly imported, add the import at the top.
  - `tests/test_spinner.py` — add a source-level regression test that greps `agent.py` for the fixed condition so the gate can't silently regress. Pattern-based, not subprocess-based, because a live-model subprocess test is too heavy for the fix surface.
- **Out**:
  - `spinner.py` itself. Its non-interactive `start()` prefix write is load-bearing for the assistant streaming header (`agent.py:1558-1559`) — removing it there would strip the `Assistant:` label from non-interactive logs, a regression. The fix has to be at the spinner's caller, and only for the tool path.
  - The `preparing tool calls` spinner at `agent.py:1594-1595`. In non-interactive mode it writes `{DIM}  preparing tool calls ` and never cleans up, which is its own minor noise — but it only fires when the model streams text *before* tool calls, which did not happen in this probe. Metric is unprovable here; leave for a separate cycle with a probe that triggers text-then-tools streaming.
  - Any change to `callbacks.py`, `theme.py`, or `ConsoleCallback.on_tool_start`. The callback is already correct — it's the caller's redundant spinner that is the problem.
  - Any visual change for interactive TTY users. The spinner animation and CLEAR_LINE cleanup stay exactly as-is when color is on.

## Implementation steps

1. Open `agent.py` at line 1785. Change
   ```python
   use_spinner = func_name not in _STREAMING_TOOLS
   ```
   to
   ```python
   use_spinner = func_name not in _STREAMING_TOOLS and not theme._no_color()
   ```
2. Confirm `theme` is already imported at module scope in `agent.py`. If not, add `import theme` at the top of the file near the other imports. (Check: `grep -n '^import theme\|^from theme' agent.py`.)
3. Run `python3 -m unittest discover tests 2>&1 | tail -5` from the worktree root and confirm all 122 tests still pass.
4. Open `tests/test_spinner.py`. Append a new test class (or a new method on `TestSpinnerInteractivity`) named `test_use_spinner_gated_on_interactive_in_agent_py` that:
   - Reads `agent.py` as text.
   - Asserts the text contains `use_spinner = func_name not in _STREAMING_TOOLS and not theme._no_color()` (or an equivalent regex allowing minor whitespace variation) so a future refactor that drops the gate fails this test.
5. Re-run `python3 -m unittest discover tests 2>&1 | tail -5`. Expect 123 tests, all passing.
6. Re-run the P-count probe against the worktree's `agent.py` from a fresh empty dir:
   ```bash
   rm -rf /tmp/probe-0015-after && mkdir -p /tmp/probe-0015-after && \
     cd /tmp/probe-0015-after && \
     timeout 400 python3 -u /tmp/agent-cicd/0015-tool-spinner-noninteractive/agent.py -a \
       "Count the number of functions whose names start with 'def test_' across all files in /mnt/droid/repos/agent/tests/. Report a single integer total." \
       > /tmp/agent-cicd/probes/0015-pcount-after.log 2>&1
   ```
7. Compute the metric delta:
   ```bash
   grep -cE '^  -> \w+ +-> \w+\(' /tmp/agent-cicd/probes/0015-pcount-before.log  # expect 1
   grep -cE '^  -> \w+ +-> \w+\(' /tmp/agent-cicd/probes/0015-pcount-after.log   # expect 0
   grep -cE '^  -> \w+\(' /tmp/agent-cicd/probes/0015-pcount-after.log           # expect ≥ 1
   ```
8. If the after-run happens to make zero non-`think` tool calls (model answered without tools), the sanity gate fails and the metric is unprovable. In that case, re-run step 6 once more with the same prompt. If still zero-tool, the probe prompt is wrong for this fix — escalate to a new probe with a forced tool call.

## Test plan

- **Existing tests that must stay green**: all 122 under `tests/`. In particular `tests/test_spinner.py`'s `test_status_non_interactive_writes_prefix_once` — the fix is in `agent.py`, not `spinner.py`, so that assertion about spinner's own non-interactive behavior must remain unchanged (start() still writes the prefix for the assistant-streaming caller).
- **New test**: `tests/test_spinner.py::test_use_spinner_gated_on_interactive_in_agent_py` — one grep-based assertion on `agent.py` source. No subprocess, no mocking of the full tool loop. This is a regression guard, not a behavioral test of the tool loop itself.
- **Re-run probe**: P-count against an empty tempdir against the worktree's `agent.py`. Expected delta: duplicated-prefix line count drops from 1 to 0 while the clean tool-start line count stays ≥ 1.

## Risks & mitigations

- **Risk**: the P-count probe is model-driven and the after-run could produce 0 non-`think` tool calls, making the sanity gate fail and the metric technically 0-to-0 ("no duplication, but no tool call either"). **Mitigation**: step 8 of the Implementation retries once, and the P-count prompt specifically asks to count lines across files in a real directory — on gemma-4-31B this has consistently produced at least one `exec_command` or `search_files` call across prior cycles (see cycles 0002, 0013). The retry budget keeps us inside the Phase 7 3-iteration debug cap.
- **Risk**: `theme._no_color()` has side effects or is expensive. **Mitigation**: `theme.py:_no_color` reads environment variables and cached flags — it's a pure O(1) function already called from every `pulse_escape` and `CLEAR_LINE` evaluation. No new side effects.
- **Risk**: suppressing the spinner in non-interactive mode breaks a user who depends on seeing *something* between `Executing N tool call(s)...` and the tool result. **Mitigation**: `on_tool_start` (callback) still fires at `agent.py:1821`, prints the full `  -> name(args)` line, and is *unchanged*. Readers lose nothing — they see the same information once instead of twice.
- **Risk**: the grep-based regression test is brittle to whitespace / variable rename. **Mitigation**: use a regex with `\s+` instead of literal spaces and match on the semantic token sequence (`use_spinner`, `_STREAMING_TOOLS`, `_no_color`), not the exact string. If a future refactor renames `use_spinner` → `spin_ok`, the test fails loudly, the author sees the guard, and updates it in the same commit that does the rename.

## Rollback

```bash
cd /tmp/agent-cicd/0015-tool-spinner-noninteractive
git checkout cicd/0015-tool-spinner-noninteractive -- agent.py tests/test_spinner.py
```

or simply revert the implementation commit. The change is one added conjunct in a boolean expression plus one new test method — both trivially reversible.

## Closes

Closes #34
