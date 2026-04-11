# 0013 — summarizer-status-dedup — results

- Issue: #26
- Branch: cicd/0013-summarizer-status-dedup
- PR: (pending — opened in Phase 8)
- Commit range: 8426fef..HEAD
- Date: 2026-04-11

## Metric

`grep -cE "Async summarizer enabled|\[summary model online at" <probe-log>`

- Baseline: **2** (from `/tmp/agent-cicd/probes/0013-pcount-before.log`)
- After:    **1** (from `/tmp/agent-cicd/probes/0013-pcount-after.log`)
- Delta:    **−1 (−50%)**

Only the `ConsoleCallback.on_summarizer_status("online", url)` render remains on stdout (`  [summary model online at http://127.0.0.1:8082]`). The former `log.info` counterpart (`INFO: Async summarizer enabled → http://127.0.0.1:8082`) is gone from the INFO console handler.

**Sanity check** — the event is still captured in the session log file via the DEBUG file handler:

```bash
$ grep -c "Async summarizer enabled" /tmp/probe-0013-after/.agent/history/session_*.log
1
```

so post-mortem debugging is not lost; the event just stops duplicating on stdout.

## Test suite

- Before: 120 passing
- After:  120 passing (no new test file — the existing `tests/test_agent_console_dedup.py` BANNED_AT_INFO list was extended by one entry so the existing two test methods now guard the new template too)

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0013-pcount-after.log`
- Verdict: **PASS** — the agent correctly reports 120 test methods with a complete breakdown by file, matching the ground truth `grep -c '^    def test_' tests/*.py` (2+21+8+13+2+7+2+2+17+8+2+1+7+28 = 120).

## What I actually changed

- `agent.py:1224` — one-word demote: `log.info("Async summarizer enabled → %s", summary_url)` → `log.debug(...)`. Nothing else on the line or its neighbors moved. The `_emit("on_summarizer_status", "online", summary_url)` call on the very next line is unchanged and continues to render the user-visible callback line.
- `tests/test_agent_console_dedup.py` — one new entry in `BANNED_AT_INFO` (`'"Async summarizer enabled → %s"'`). The cycle-0008 source-level guard now covers the new template with zero new test methods.

That's it. Two files, two tiny diffs, one commit each (plus the Phase-8 paperwork commit).

## What I learned

- **Cycle 0008's scope gate missed this one because the callback method was dedicated, not generic.** Cycle 0008 audited the `on_notice` / user-message / assistant / tool-call / tool-result surface but left per-event callbacks like `on_summarizer_status`, `on_continue_none`, `on_cycle_bumped`, and the `_condense_summary` info lines untouched. There are more duplicates on that list — they're good candidates for future tight cycles, one per probe.
- **Startup-line metrics are zero-variance.** The P-count probe's startup lines printed identically on both the baseline and the after-run. That matches prior cycles 0003/0007/0009 — any metric measured on session-header output is deterministic and doesn't need multi-sample averaging.
- **The 1-call P-count probe has a lurking inefficiency**: the agent ran `grep` twice because its first command didn't compute a total. That's a model-habit issue, not a tool-description issue — a probe-driven cycle targeting "test-count task runs in exactly 1 tool call" would need either a custom helper tool or a system-prompt nudge, and the measurement would be 2→1 tool calls with zero variance since the probe is pure bash. Not urgent; filing as a follow-up thought for a future PROBE but not as an issue yet.
- **Side-sightings worth future issues (not filed this cycle to avoid queue spam)**: `agent.py` still has ~5 similar `log.info`/`log.warning`-paired-with-`_emit` duplicates on the summarize / continue / cycle-bump paths. The cleanest way to drain them is one per cycle with a dedicated probe that exercises that path, not a bulk audit — bulk demote is tempting but risks changing warn-level semantics for paths the probe doesn't exercise.
