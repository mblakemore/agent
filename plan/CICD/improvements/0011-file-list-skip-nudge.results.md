# 0011 — file-list-skip-nudge — results (NULL-RESULT)

- Issue: #21
- Branch: cicd/0011-file-list-skip-nudge (deleted — null-result path)
- PR: none (null-result — no PR opened)
- Commit range: 662de7c..3b0d60e on the deleted branch (kept locally only during the attempt)
- Date: 2026-04-11

## Verdict

**NULL-RESULT.** The cycle could not demonstrate a measurable improvement. The `P-bug` probe's tool-call count has run-to-run variance of at least ±3 on this model (gemma-4-31B via llama-server), which swamps the 1-call delta the nudge was designed to produce. I do not have evidence the nudge moved the metric; I cannot claim a win.

## Metric

**Target**: reduce tool-call count on P-bug from 5 → ≤4.

**Baseline** — 3 runs against stock `agent.py` at HEAD `5bda33f` on a fresh-seeded `running_max.py` + `test_running_max.py`:

| Run | Tool calls | First call |
|---|---|---|
| 1 | 5  | `file(action='list', path='.')` |
| 2 | 8  | `file(action='list', path='.')` |
| 3 | 10 | `file(action='list', path='.')` |

Mean: **7.67**. Range: **5–10**. First-run baseline of 5 was an anomaly — it was the lowest of the three.

**After** (strengthened final nudge, `IMPORTANT:` clause at the end of the `file` tool description mentioning `list`, `ls`, `find`, `tree` plus matching clause in `exec_command` description) — 3 runs against the worktree's `agent.py`:

| Run | Tool calls | First call |
|---|---|---|
| 1 | 6  | `exec_command(command='ls -R')` |
| 2 | 5  | `file(action='list', path='.')` |
| 3 | 11 | `file(action='list', path='.')` |

Mean: **7.33**. Range: **5–11**.

**Delta (mean)**: −0.33 (−4.3%). **Delta (min)**: 0. **Delta (max)**: +1 (worse).

A 0.33-call mean delta on a metric with ±3 sample-to-sample variance is not a measurable improvement. Under any honest read, this is noise.

## Test suite

- Before: 119 passing
- After the nudge edits (while branch existed): 120 passing (new `TestFileListDescriptionDiscouragesWastedOrient::test_list_description_discourages_wasted_orient`)
- Current (after null-result revert): 119 passing, back at stock

## Probe re-runs

All logs under `/tmp/agent-cicd/probes/`:

- `0011-pbug-before.log` — first baseline run, 5 calls, PASS
- `0011-pbug-before-2.log` — second baseline run, 8 calls, PASS
- `0011-pbug-before-3.log` — third baseline run, 10 calls, PASS
- `0011-pbug-after.log` — first after-run with the edit-1 nudge (file.py only), 5 calls, **substituted** `exec_command('ls -F')` for `file(list)` — nudge landed but friction migrated
- `0011-pbug-after-1.log` — first strengthened-nudge run, 6 calls, `exec_command('ls -R')` first
- `0011-pbug-after-2.log` — second strengthened-nudge run, 5 calls, `file list` first (nudge ignored)
- `0011-pbug-after-3.log` — third strengthened-nudge run, 11 calls, `file list` first (nudge ignored)

## What I actually changed (then reverted)

During the 3 debug iterations the branch held:

- `tools/file.py` — `list` action bullet rewritten and a trailing `IMPORTANT:` clause added, mentioning both `list` and shell `ls`/`find`/`tree` to discourage starting a task by orienting.
- `tools/exec_command.py` — trailing sentence appended discouraging orient-first `ls`/`find`/`tree`.
- `tests/test_file_tool.py` — new `TestFileListDescriptionDiscouragesWastedOrient` regression test asserting the `"already named"` and `"read them directly"` substrings survive in the `file` tool description.

All three edits were discarded with the branch deletion. Stock `tools/file.py`, `tools/exec_command.py`, and `tests/test_file_tool.py` are unchanged by this cycle. The **only** trace on main is this paperwork commit.

## What I learned

- **The P-bug probe's tool-call count is not a reliable metric for 1-call improvements on this model.** 3 baseline runs spanned 5–10 calls (a 2x range). A 1-call delta can't be distinguished from run-to-run variance at n=3. Future cycles that target a 1-call improvement should either pick a probe with tighter variance or commit to n ≥ 10 runs per arm — both of which cost significantly more wall time than a cheap cycle wants.
- **The "orient-first" reflex is deep on gemma-4-31B.** In 5 of 6 post-nudge runs the agent still issued either `file(list)` or an `exec_command` directory-listing (`ls`, `ls -R`, `ls -F`) as its first tool call. Adding an `IMPORTANT:` clause to the `file` and `exec_command` descriptions was not enough to override the instinct. Cycles 0004 (auto-mkdir advertise) and 0009 (search_files path warning) landed with the same shape of nudge on tighter probes — the difference here is that "orient first" is a higher-level behavior than "call tool X with parameter Y," and probably sits closer to a plan-shape prior than a tool-choice prior. A stronger fix might need to be at the system-prompt level, or might need a structural change (e.g. surface the first-turn prompt differently so there's less ambiguity about whether the files exist).
- **Nudge migration is a real failure mode.** On the first after-run (only `file.py` edited) the agent correctly skipped `file(list)` — but substituted `exec_command('ls -F')` in the same slot. The nudge changed *which* tool it used to orient, not *whether* it oriented. A CICD nudge that targets one tool in isolation can just push the friction into a neighbouring tool; future nudges in this family should probably land in both places at once (or higher up the stack).
- **Single-run baselines are dangerous for small-delta cycles.** Had I locked the 5-call baseline from run 1 and called it a day, the strengthened-nudge run 2 (5 calls) would have looked like "no change" and run 3 (11 calls) like a regression. Only collecting 3 samples on each side revealed the true picture: both distributions overlap almost entirely. Lesson for the loop: **for a < 20% expected delta, always run ≥ 3 samples on both baseline and after before declaring.** For cycles with ≥ 50% expected delta (cycles 0002, 0005, 0006 all cleared 100%) a single run is still fine.
- **Null-result is the right verdict here, not a small measured win.** The nudge does not harm anything and plausibly helps, but "plausibly helps" is explicitly not the bar the loop sets. Hard rule #1 says "no unmeasured wins" and I don't have a measured win to report.

## Rollback

Already done. The `cicd/0011-file-list-skip-nudge` branch has been deleted; the worktree is removed; all nudge edits are gone. `tools/file.py`, `tools/exec_command.py`, and `tests/test_file_tool.py` are back at HEAD `5bda33f`. Only the paperwork (this file, the plan, and the progress-log row) remains on main.
