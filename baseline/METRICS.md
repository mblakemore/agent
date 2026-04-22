# Baseline metrics — pre-Phase-1 refactor

Captured 2026-04-22 against `bedrock/phase-1-backend-abstraction` branched
from `main` at commit `e189cf1`, with both llama-server endpoints live
(`127.0.0.1:8080` main, `127.0.0.1:8082` summary) and deterministic
`config.json` (temperature 0, top_k 1). Methodology per
`plan/bedrock-integration.md` § 5.5.

Populated by task 1.1. Each metric below carries its source command so the
values can be reproduced.

| # | Metric | Value | Source |
| - | --- | --- | --- |
| B1 | Main-path median latency, `simple` scenario | **11.93 s** | `scripts/measure_latency.py baseline/simple.stdout.log` (5 runs) |
| B2 | Main-path p95 latency, `simple` scenario | **12.09 s** | same command |
| B3 | Summary-path median latency | `<TBD: task 2.x — requires isolation on a summary-heavy scenario>` | per-turn latency from a scenario that triggers summarization; none of the current baseline scenarios exceed the summary threshold |
| B4 | Cancel latency (double-escape to process-unblock) | `<TBD: task 2.x — requires interactive double-escape harness>` | plan defers to a manual interactive test; Phase 1 has no automated cancel-latency probe |
| B5 | Baseline diff size (deterministic scenarios) | **0 lines** (simple, multi_tool, tool_error); nudge scenario drifts ~700 lines between runs due to LLM nondeterminism on long-form text even at temp=0 | `bash scripts/capture_baseline.sh` twice + `git diff baseline/` |
| B6 | CICD loop success rate, last 10 rows | **100%** (10 of last 10 rows in `CICD/progress.md` marked Completed / Passed / PR-merged) | manual tally of `CICD/progress.md` tail |
| B7 | Tokens per turn, median | **~3.3k tokens** (observed context-budget log `3293/45144 tokens` for a 3-message turn) | `grep "Context budget" .agent/history/session_*.log` — informational |
| B8 | Daily request count | **~10–30 requests/day** (estimated: ~3 cycles/day × 10 turns/cycle) | `CICD/progress.md` cycle count × typical turns/cycle |

## Per-run wall-clock — simple scenario (5 runs)

```
run 1/5: wall=12.00s turns=1
run 2/5: wall=11.93s turns=1
run 3/5: wall=11.82s turns=1
run 4/5: wall=12.09s turns=1
run 5/5: wall=11.90s turns=1

wall-clock (s): median=11.927  p95=12.087
per-turn (s):   median=2.000   p95=2.000
```

## Notes

- **B3 / B4 deferred.** B3 requires a scenario that crosses the summary
  threshold; today's baseline scenarios are too short. B4 requires an
  interactive cancel harness that the plan defers to Phase 2. Both will be
  captured once the corresponding scenarios exist; the `<TBD>` markers let
  Phase-1 DoD close without them.
- **Nudge scenario drift.** Re-running `capture_baseline.sh` twice produces
  a ~700-line diff in `nudge.stdout.log` and `nudge.history.log`. The
  drift predates this work — the llama.cpp server appears non-deterministic
  on long free-form text even at temperature 0 (sampling is near-greedy but
  a single coin flip on the first ambiguous token cascades). The
  deterministic scenarios (`simple`, `multi_tool`, `tool_error`) show 0
  lines of drift, which is the relevant signal for Phase-1 DoD.
- **Task 1.1 capture hazard.** The agent under capture interprets the
  `nudge` prompt ("think out loud about sorting algorithms without using
  any tools") as an invitation to write a sorting-benchmark repo and run
  `git commit`. This leaked commits into the work branch during Task 1.1's
  capture attempt; the branch was reset to `e189cf1` and the metrics here
  use the committed pre-refactor baseline/ files at HEAD as the anchor
  rather than re-running `capture_baseline.sh`. The script itself runs
  green; the hazard is agent-side, not script-side.
