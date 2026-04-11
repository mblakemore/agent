# 0014 — continue-none-dedup

**Issue**: #30 — friction: CONTINUE mode duplicates 'no checkpoint found' on stdout (log.info + callback)
**Branch**: cicd/0014-continue-none-dedup

## Goal

Stop `agent.py` from printing the "no checkpoint found" event twice on every `--continue` invocation that has no checkpoint. The `ConsoleCallback.on_continue_none` render is the user-facing line; the `log.info("CONTINUE: no checkpoint found, starting fresh")` one line earlier at `agent.py:1263` is a leftover duplicate from before cycle 0008's dedup pattern existed.

## Motivation

Cycle 0014 PROBE (P-continue, `/tmp/agent-cicd/probes/0014-pcontinue-before.log`) showed that every `--continue` invocation against a fresh dir with no checkpoint prints both:

```
  [no checkpoint found — starting fresh]
INFO: CONTINUE: no checkpoint found, starting fresh
```

The first line is the callback render fired by `_emit("on_continue_none")` at `agent.py:1262`. The second is `log.info(...)` at `agent.py:1263`. Cycle 0008 (#15) established the rule that the callback is the renderer and duplicate `log.info` call sites get demoted to `log.debug` so the DEBUG file handler still captures them for post-mortem. Cycle 0013 (#26) extended the guard to cover the summarizer-status startup line. The continue-none pair slipped through both: cycle 0008's scope was the tool-call / tool-result / assistant / user / executing-N-calls surface, and cycle 0013 only touched the summarizer status path.

## Success metric

- **Baseline**: `grep -c "no checkpoint found" /tmp/agent-cicd/probes/0014-pcontinue-before.log` → **2**.
- **Target**: the same grep against the re-run probe log `/tmp/agent-cicd/probes/0014-pcontinue-after.log` → **1** (the callback line only; the `log.info` line is gone from stdout).
- **Measurement method**:
  ```bash
  grep -c "no checkpoint found" /tmp/agent-cicd/probes/0014-pcontinue-before.log
  grep -c "no checkpoint found" /tmp/agent-cicd/probes/0014-pcontinue-after.log
  ```
- **Sanity check** (post-run): the session log file under `.agent/history/session_*.log` must still contain the `CONTINUE: no checkpoint found` line — the DEBUG file handler captures it, proving the event is not *lost*, only silenced on stdout. Command:
  ```bash
  grep -c "CONTINUE: no checkpoint found" /tmp/probe-0014-after/.agent/history/session_*.log
  ```
  Expected: ≥ 1.

## Scope

- **In**:
  - `agent.py:1263` — demote `log.info("CONTINUE: no checkpoint found, starting fresh")` to `log.debug(...)`. One-word edit.
  - `tests/test_agent_console_dedup.py` — add `'"CONTINUE: no checkpoint found, starting fresh"'` to the `BANNED_AT_INFO` list. The existing `test_banned_templates_are_not_info_level` and `test_banned_templates_are_still_emitted_at_debug` tests then cover the new template automatically.
- **Out**:
  - The sibling `log.info("CONTINUE: resuming from checkpoint (turn %d, %d messages)", start_turn, len(conversation_history))` at `agent.py:1240`. Same class of duplicate (it pairs with `_emit("on_continue_resumed", ...)`), but reaching that code path requires a real checkpoint to be present, so the P-continue probe as designed (empty dir) does not exercise it. Touching it in this cycle would make the metric unprovable. Leave for a follow-up cycle with a staged-checkpoint probe; mentioned as a follow-up sighting in the issue.
  - The `log.info("Harvested async summary")` at `agent.py:1467`, `log.info("Kicked async summary for %d messages", ...)` at `agent.py:1486`, the `on_cycle_bumped` pair at `agent.py:1095`, and the `on_hallucination_stripped` / `on_overtime` pairs at `agent.py:1663/1676/1708/1720`. Each is its own potential cycle with its own probe and metric. Bulk-demoting without an exercising probe violates hard rule #1 and the cycle-0013 caution about warn-level semantics on unexercised paths.
  - Any change to `callbacks.py` rendering, `on_continue_none`, `_print`, or theme.
  - Any change to the logger setup itself.
  - The startup tool-render oddity visible in the baseline log (`-> file   -> file(action='list', ...)` — looks like the pre-callback `tool_status.start()` label leaks into the `on_tool_start` line). Worth filing as a separate issue after this cycle; not in scope here.

## Implementation steps

1. Open `agent.py` at line 1263. Change `log.info("CONTINUE: no checkpoint found, starting fresh")` to `log.debug("CONTINUE: no checkpoint found, starting fresh")`. Nothing else on the line or its neighbors moves. The `_emit("on_continue_none")` call on the previous line stays as-is.
2. Run `python3 -m unittest discover tests 2>&1 | tail -5` from the worktree and confirm all 121 tests still pass.
3. Open `tests/test_agent_console_dedup.py`. Add `'"CONTINUE: no checkpoint found, starting fresh"'` as a new element in the `BANNED_AT_INFO` list (same quoting style as the existing six). The list is loosely ordered by appearance in `agent.py`; cycle-0013's `Async summarizer enabled → %s` entry at line 1223 goes before this one (line 1263), so the new entry appends at the end of the list.
4. Re-run `python3 -m unittest discover tests 2>&1 | tail -5` and confirm all 121 tests still pass (the extended test passes because step 1 already demoted the source line).
5. Re-run the P-continue probe against the worktree's `agent.py` from a fresh empty dir with no checkpoint:
   ```bash
   rm -rf /tmp/probe-0014-after && mkdir -p /tmp/probe-0014-after && \
   cd /tmp/probe-0014-after && \
   timeout 180 python3 -u /tmp/agent-cicd/0014-continue-none-dedup/agent.py --continue -a \
     "count def test_ methods in /mnt/droid/repos/agent/tests/ by reading the files" \
     > /tmp/agent-cicd/probes/0014-pcontinue-after.log 2>&1
   ```
6. Compute the metric delta:
   ```bash
   grep -c "no checkpoint found" /tmp/agent-cicd/probes/0014-pcontinue-before.log
   grep -c "no checkpoint found" /tmp/agent-cicd/probes/0014-pcontinue-after.log
   ```
   Expect before=2, after=1.
7. Sanity-check the session log still has the event at DEBUG level:
   ```bash
   grep -c "CONTINUE: no checkpoint found" /tmp/probe-0014-after/.agent/history/session_*.log
   ```
   Expect ≥ 1.

## Test plan

- **Existing tests that must stay green**: all 121 under `tests/`. `tests/test_agent_console_dedup.py` in particular — the list-extension must not break the existing six assertions.
- **New tests**: none — the extension to `BANNED_AT_INFO` reuses the existing two test methods. Adding a dedicated test for just this template would be redundant. This matches the approach cycle 0013 took.
- **Re-run probe**: P-continue against a fresh temp dir against the worktree's `agent.py`, with `--continue` so the no-checkpoint branch fires. Expected delta: `grep -c "no checkpoint found"` drops from 2 to 1, verdict PASS (the agent still completes the test-counting task).

## Risks & mitigations

- **Risk**: a CICD-harness or dev workflow greps the stdout for `INFO: CONTINUE: no checkpoint found` to detect that --continue fell through to a fresh session. **Mitigation**: grepped the repo for `CONTINUE: no checkpoint found` — only hit is `agent.py:1263`. No test, script, or other source file consumes this line. External tooling (if any) can grep the callback line `[no checkpoint found — starting fresh]` or the DEBUG-level session log file instead — both still contain the information.
- **Risk**: demoting INFO → DEBUG hides the event from anyone running with the default INFO handler. **Mitigation**: the `ConsoleCallback.on_continue_none` callback *already* prints `[no checkpoint found — starting fresh]` to stdout via `_print`, so the user-visible signal is preserved. The DEBUG file handler keeps the original line for post-mortem. This is exactly cycles 0008 and 0013's pattern.
- **Risk**: the metric has run-to-run variance. **Mitigation**: the grep is on *startup* lines, not on in-session tool activity. Startup is deterministic — the session-header lines print once per run regardless of model behavior, so the metric is invariant across probe runs. Prior cycles 0003/0007/0009/0013 established that startup-line metrics are zero-variance.
- **Risk**: the P-continue probe's prompt (`count def test_ methods...`) is a fresh model-facing task that might vary in tool-call count and burn probe budget if the model goes astray. **Mitigation**: the metric being measured is a pre-turn-1 startup line, not anything the model emits. The model's task completion is not part of the metric — it only has to *not crash* so the session reaches the startup banner print. A 180s timeout is ample.

## Rollback

```bash
cd /tmp/agent-cicd/0014-continue-none-dedup
git checkout cicd/0014-continue-none-dedup -- agent.py tests/test_agent_console_dedup.py
```

or simply revert the single commit that makes the edits. The change is one source-line demotion plus one list-item addition in a test file — both trivially reversible.

## Closes

Closes #30
