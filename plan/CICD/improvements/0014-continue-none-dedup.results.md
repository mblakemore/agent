# 0014 — continue-none-dedup — results

- Issue: #30
- Branch: cicd/0014-continue-none-dedup
- PR: (pending — opened in Phase 8)
- Commit range: 5b5fec8..HEAD
- Date: 2026-04-11

## Metric

`grep -c "no checkpoint found" <probe-log>`

- Baseline: **2** (from `/tmp/agent-cicd/probes/0014-pcontinue-before.log`)
- After:    **1** (from `/tmp/agent-cicd/probes/0014-pcontinue-after.log`)
- Delta:    **−1 (−50%)**

Only the `ConsoleCallback.on_continue_none` render remains on stdout (`  [no checkpoint found — starting fresh]`). The former `log.info` counterpart (`INFO: CONTINUE: no checkpoint found, starting fresh`) is gone from the INFO console handler.

**Sanity check** — the event is still captured in the session log file via the DEBUG file handler:

```bash
$ grep -c "CONTINUE: no checkpoint found" /tmp/probe-0014-after/.agent/history/session_*.log
1
```

so post-mortem debugging is not lost; the event just stops duplicating on stdout.

## Test suite

- Before: 121 passing
- After:  121 passing (no new test file — the existing `tests/test_agent_console_dedup.py` BANNED_AT_INFO list was extended by one entry so the existing two test methods now guard the new template too)

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0014-pcontinue-after.log`
- Verdict: **PASS** — the agent correctly reports 121 `def test_` methods in `tests/`, matching the ground truth `grep -rc '^    def test_' tests/*.py` total. Note: the after-run finished the task in 2 turns (file list → exec_command grep) vs. the baseline's longer path, but that is model run-to-run variance on the task, not anything the metric measures.

## What I actually changed

- `agent.py:1263` — one-word demote: `log.info("CONTINUE: no checkpoint found, starting fresh")` → `log.debug(...)`. Nothing else on the line or its neighbors moved. The `_emit("on_continue_none")` call on the previous line is unchanged and continues to render the user-visible callback line.
- `tests/test_agent_console_dedup.py` — one new entry in `BANNED_AT_INFO` (`'"CONTINUE: no checkpoint found, starting fresh"'`). The cycle-0008 source-level guard now covers the new template with zero new test methods.

That's it. Two files, two tiny diffs, one commit each (plus the Phase-8 paperwork commit).

## What I learned

- **The `--continue` path's other duplicate (`on_continue_resumed` at `agent.py:1240`) is structurally identical but needs a staged checkpoint to exercise.** A future probe should write a fake checkpoint JSON to `.agent/state/checkpoint.json` (or whatever the actual path is) and then run `--continue` to land on the resume branch. That's a 5-minute probe setup that unlocks another zero-variance cycle. Worth filing as a follow-up issue.
- **Side-sighting, not filed this cycle**: the baseline probe log line 22 shows `  -> file   -> file(action='list', path='...')` — the tool-call line has a duplicated `-> file` prefix. Looks like `tool_status.start()` writes a short label then `on_tool_start` writes the full form without clearing; `theme.CLEAR_LINE` is in the format string but apparently doesn't reach the duplicate. Reproducible on every multi-tool session. Worth its own cycle with a dedicated probe that counts `-> <name>\s+-> <name>(` repetitions.
- **The follow-up duplicate list from cycle 0013's notes is still ~7 entries deep** (`on_cycle_bumped`, `on_continue_resumed`, `on_summary_ready`, `on_notice` for background summarization, `on_overtime`, `on_hallucination_stripped` ×2). Each is its own one-line demote with its own exercising probe. Resist the bulk-demote temptation — hard rule #1 requires the probe to exercise the path.
- **Startup-line metrics remain zero-variance across cycles.** Confirmed again: the before-log line 14–15 sequence and the after-log line 14 sequence are byte-identical except for the removed line. That matches cycles 0003/0007/0009/0013.
