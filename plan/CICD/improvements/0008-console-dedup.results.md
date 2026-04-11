# 0008 — console-dedup — results

- Issue: #15
- Branch: cicd/0008-console-dedup
- PR: (to be filled after `gh pr create`)
- Commit range: 6dc1e81..HEAD
- Date: 2026-04-11

## Metric

- **Baseline**: 206 lines in `/tmp/agent-cicd/probes/0008-bug-before.log` (P-bug probe, HEAD=ed6fa2f).
- **After**: 104 lines in `/tmp/agent-cicd/probes/0008-bug-after.log` (P-bug probe, worktree HEAD, 6 tool calls — one more than baseline).
- **Delta**: −102 lines (−49.5%). Well past the ≤ 155 (−25%) target. Actual reduction beats the stretch prediction (~130) because the biggest offender — the 50-line `exec_command` failing-test dump that appeared twice in baseline — is now rendered exactly once via the callback's truncated form.

Secondary counts on the after-log:

| Event | INFO lines (before fix) | INFO lines (after fix) | Callback lines (unchanged) |
|---|---|---|---|
| TOOL RESULT / Result: | 5 | 0 | 6 |
| TOOL CALL / -> | 5 | 0 | 6 |
| ASSISTANT / Assistant: | 1 (multi-line dump) | 0 | 1 (stream) |
| USER / You: | 1 | 0 | 1 |
| Executing N tool call(s) | 5 | 0 | 5 |

## Test suite

- **Before**: 114 passing (`python3 -m unittest discover tests` at HEAD=ed6fa2f).
- **After**: 116 passing (114 pre-existing + 2 new in `tests/test_agent_console_dedup.py`).

## Probe re-run

- **Log**: `/tmp/agent-cicd/probes/0008-bug-after.log`
- **Verdict**: PASS — agent diagnosed the `+2 → +1` bug, corrected it, and re-ran tests to green in 6 tool calls (1 extra vs baseline, well within run-to-run variance).
- **Session log file sanity check**: `/tmp/probe-0008-bug/.agent/history/session_*.log` still contains 6× `TOOL RESULT`, 6× `TOOL CALL`, 1× `ASSISTANT`, 1× `USER` entries at DEBUG — the full untruncated post-mortem record is intact. The DEBUG file handler (level DEBUG) picks up the demoted calls; only the INFO console handler drops them. As designed.

## What I actually changed

- `agent.py`: six one-word edits demoting `log.info` → `log.debug` at the five duplicated event sites (USER has two call sites; the other four have one each).
- `tests/test_agent_console_dedup.py`: new regression test. Source-text assertion that the five banned templates appear only as `log.debug(...)` (not `log.info(...)`), and that they are still present at `log.debug(...)` so the session log file keeps them. Same shape as the cycle 0005/0006 SHARED RUNTIME regression tests.

## What I learned

- **Cycle budget**: the whole thing was ~6 edits and a 65-line test. The signal-to-diff ratio on logging hygiene is excellent — this class of friction (duplicate render paths) is probably lurking elsewhere in the tree.
- **Baseline choice**: picking line count of a real probe log as the metric turned out to be a clean proxy for console noise. Every future CICD probe log will be ~50% smaller for free, making them faster to grep and easier to read.
- **Callback vs logger layering**: `ConsoleCallback` is the intended user-facing renderer with truncation and theming; the logger's console handler was doing a second, untruncated render that defeated the callback's `compact_limit` truncation entirely. Worth checking other `log.info` call sites in future cycles for similar overlap, though the remaining INFO lines (session/turn/status events) are genuinely status-level and should stay.
