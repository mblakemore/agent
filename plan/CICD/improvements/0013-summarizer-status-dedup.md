# 0013 — summarizer-status-dedup

**Issue**: #26 — friction: 'Async summarizer enabled' logs twice on every session startup
**Branch**: cicd/0013-summarizer-status-dedup (will be created in Phase 6)

## Goal

Stop `agent.py` from printing the summarizer-status event twice on every session startup. The `ConsoleCallback.on_summarizer_status("online", url)` render is the user-facing line; the `log.info("Async summarizer enabled → %s", summary_url)` one line earlier is a leftover duplicate from before cycle 0008's dedup pattern existed.

## Motivation

Cycle 0013 PROBE (P-count, `/tmp/agent-cicd/probes/0013-pcount-before.log`) showed that every session start prints both:

```
INFO: Async summarizer enabled → http://127.0.0.1:8082
  [summary model online at http://127.0.0.1:8082]
```

The first line is `log.info` at `agent.py:1224`. The second is the callback render fired by `_emit("on_summarizer_status", "online", summary_url)` on the next line. Cycle 0008 (#15) established the rule that the callback is the renderer and duplicate `log.info` call sites get demoted to `log.debug` so the DEBUG file handler still captures them for post-mortem. The summarizer-status pair slipped through cycle 0008's scope because cycle 0008 only targeted tool-call / tool-result / assistant / user / executing-N-calls templates; the summarizer path uses a different callback method (`on_summarizer_status`) and was never audited.

Every one of the committed `baseline/*.stdout.log` files shows this duplication (lines 14–15), so it's been in the tree since cycle 0008 landed.

## Success metric

- **Baseline**: `grep -cE "Async summarizer enabled|\[summary model online at" /tmp/agent-cicd/probes/0013-pcount-before.log` → **2**.
- **Target**: the same grep against the re-run probe log `/tmp/agent-cicd/probes/0013-pcount-after.log` → **1** (the callback line only; the `log.info` line is gone from stdout).
- **Measurement method**:
  ```bash
  grep -cE "Async summarizer enabled|\[summary model online at" /tmp/agent-cicd/probes/0013-pcount-before.log
  grep -cE "Async summarizer enabled|\[summary model online at" /tmp/agent-cicd/probes/0013-pcount-after.log
  ```
- **Sanity check** (post-run): the session log file under `.agent/history/session_*.log` must still contain the `Async summarizer enabled` line — the DEBUG file handler captures it, proving the event is not *lost*, only silenced on stdout. Command:
  ```bash
  grep -c "Async summarizer enabled" /tmp/probe-0013-after/.agent/history/session_*.log
  ```
  Expected: ≥ 1.

## Scope

- **In**:
  - `agent.py:1224` — demote `log.info("Async summarizer enabled → %s", summary_url)` to `log.debug(...)`. One-word edit.
  - `tests/test_agent_console_dedup.py` — add `'"Async summarizer enabled → %s"'` to the `BANNED_AT_INFO` list. The existing `test_banned_templates_are_not_info_level` and `test_banned_templates_are_still_emitted_at_debug` tests then cover the new template automatically.
- **Out**:
  - The two sibling `log.warning` calls at `agent.py:1227` and `:1231` (`Summary endpoint returned %d`, `Summary endpoint unreachable at %s`). Same root cause but different log level; demoting WARNING is a larger semantic change and the current probe doesn't exercise those paths, so the metric wouldn't prove the fix. Leave them for a follow-up issue if the creator wants them.
  - Any other `log.info`/`log.warning` sites that pair with `_emit(...)` elsewhere in `agent.py` (e.g., `_condense_summary` at line 589/606, `CONTINUE: no checkpoint found` at line 1264, `Harvested async summary` at line 1469, `Kicked async summary` at line 1488). Each is its own potential cycle with its own probe and metric.
  - Any change to `callbacks.py` rendering, `on_summarizer_status`, `_note`, or theme.
  - Any change to the logger setup itself.

## Implementation steps

1. Open `agent.py` at line 1224. Change `log.info("Async summarizer enabled → %s", summary_url)` to `log.debug("Async summarizer enabled → %s", summary_url)`. Nothing else on the line or its neighbors moves.
2. Run `python3 -m unittest discover tests 2>&1 | tail -5` and confirm all 120 tests still pass.
3. Open `tests/test_agent_console_dedup.py`. Add `'"Async summarizer enabled → %s"'` as a new element in the `BANNED_AT_INFO` list (same quoting style as the existing five). Keep the list sorted by appearance-order in `agent.py` for readability; this new template lives around line 1224 so it goes at the end of the list.
4. Re-run `python3 -m unittest discover tests 2>&1 | tail -5` and confirm all 120 tests still pass (the extended test still passes because step 1 already demoted the source line).
5. Re-run the P-count probe against the worktree's `agent.py`:
   ```bash
   mkdir -p /tmp/probe-0013-after && cd /tmp/probe-0013-after && \
   timeout 200 python3 -u /tmp/agent-cicd/0013-summarizer-status-dedup/agent.py -a \
     "Count the number of test methods (functions starting with 'def test_') across all Python files in the /mnt/droid/repos/agent/tests/ directory. Report the total count and a breakdown by file." \
     > /tmp/agent-cicd/probes/0013-pcount-after.log 2>&1
   ```
6. Compute the metric delta:
   ```bash
   grep -cE "Async summarizer enabled|\[summary model online at" /tmp/agent-cicd/probes/0013-pcount-before.log
   grep -cE "Async summarizer enabled|\[summary model online at" /tmp/agent-cicd/probes/0013-pcount-after.log
   ```
   Expect before=2, after=1.
7. Sanity-check the session log still has the event at DEBUG level:
   ```bash
   grep -c "Async summarizer enabled" /tmp/probe-0013-after/.agent/history/session_*.log
   ```
   Expect ≥ 1.

## Test plan

- **Existing tests that must stay green**: all 120 under `tests/`. `tests/test_agent_console_dedup.py` in particular — the list-extension must not break the existing five assertions.
- **New tests**: none — the extension to `BANNED_AT_INFO` reuses the existing two test methods. Adding a dedicated test for just this template would be redundant.
- **Re-run probe**: P-count against a fresh temp dir against the worktree's `agent.py`. Expected delta: `grep -cE "Async summarizer enabled|\[summary model online at"` drops from 2 to 1, verdict PASS (the agent still counts tests correctly).

## Risks & mitigations

- **Risk**: a CICD-harness or dev workflow greps the stdout for `INFO: Async summarizer enabled` to detect that summarization is active. **Mitigation**: grepped the repo for `Async summarizer enabled` — only hits are `agent.py:1224` and the `baseline/*.stdout.log` committed snapshots (which are reference artifacts, not parsed). No test, script, or other source file consumes this line. External tooling (if any) can grep the callback line `[summary model online at URL]` or the DEBUG-level session log file instead — both still contain the information.
- **Risk**: demoting INFO → DEBUG hides the event from anyone running with the default INFO handler. **Mitigation**: the `ConsoleCallback.on_summarizer_status("online", url)` callback *already* prints `[summary model online at {url}]` to stdout via `_note`, so the user-visible signal is preserved. The DEBUG file handler keeps the original line for post-mortem. This is exactly cycle 0008's pattern.
- **Risk**: the metric has run-to-run variance. **Mitigation**: the grep is on *startup* lines, not on in-session tool activity. Startup is deterministic — the session-header lines print once per run regardless of model behavior, so the metric is invariant across probe runs. Prior cycles 0003/0007/0009 established that startup-line metrics are zero-variance.
- **Risk**: the updated `baseline/*.stdout.log` reference files drift from the new reality. **Mitigation**: those files are cycle-0008 reference artifacts and are not automatically regenerated; leave them alone. They still document the pre-0008 state accurately. A follow-up cycle can refresh them if desired.

## Rollback

```bash
cd /tmp/agent-cicd/0013-summarizer-status-dedup
git checkout cicd/0013-summarizer-status-dedup -- agent.py tests/test_agent_console_dedup.py
```

or simply revert the single commit that makes the edits. The change is one source-line demotion plus one list-item addition in a test file — both trivially reversible.

## Closes

Closes #26
