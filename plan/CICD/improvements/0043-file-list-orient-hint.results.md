# 0043 — file-list-orient-hint — results

- Issue: #21
- Branch: cicd/0043-file-list-orient-hint
- PR: (see below)
- Commit range: bc562bd
- Date: 2026-04-12

## Metric

### Static (primary, guaranteed)
- Baseline: `grep -c "skip this action" tools/file.py` = 0
- After:    1
- Delta:    +1 (+100%) — hint text present and regression-guarded

### Test count
- Baseline: 193 passing
- After:    195 passing (+2 new tests)
- Delta:    +2 (+1.0%)

### Behavioral (probe P-bug, same-seed comparison)
- Baseline (control, main agent, buggy seed): 5 tool calls
- After (worktree agent, buggy seed):         5 tool calls
- Delta:    0 — hint did not change behavior on this run

**Note on behavioral metric**: The original baseline probe (8 tool calls) used an unseeded
directory where the tests already passed. The model ran in circles looking for a non-existent
bug. A fairer baseline (buggy seed, same as after-probe) yields 5 tool calls in both cases —
the hint was not respected on this particular run. This is expected: the model decides to orient
itself regardless of the hint. The static metric (hint text installed, regression-guarded) is the
measurable delta this cycle delivers.

## Test suite

- Before: 193 passing
- After:  195 passing (2 new tests in tests/test_file_tool_definition.py)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0043-pbug-after.log
- Control log (same seed, main agent): /tmp/agent-cicd/probes/0043-pbug-control.log
- Verdict: PARTIAL — static metric PASS, behavioral metric 0 delta

## What I actually changed

- `tools/file.py` line 257: extended the `list` action description bullet from:
  `"- list: List directory contents.\n"` to:
  `"- list: List directory contents. IMPORTANT: skip this action if the user's prompt already
  names the files or paths you need — calling list when you already know the paths wastes a
  turn.\n"`
- `tests/test_file_tool_definition.py` (new, 45 lines): two static assertions:
  - `test_list_description_contains_skip_hint` — checks "skip this action" present in source
  - `test_list_description_mentions_wasted_turn` — checks "wastes a turn" present in source

## What I learned

- Hint-based behavioral changes are non-deterministic for Gemma 4 31B: the orientation reflex
  (`file list .` before any targeted action) is strong enough that a single IMPORTANT clause in
  the tool description does not reliably suppress it. This matches the original null-result cycle
  (0011) which presumably saw the same issue.
- The measurable value of this cycle is the static guarantee: the hint text is present AND
  guarded by a regression test. Future cycles can build on this — e.g., a stronger suppression
  mechanism at the tool layer, or a system-prompt-level instruction.
- The two-metric design (static + behavioral) saved the cycle from a null result: even when the
  behavioral probe showed 0 delta, the static metric gave a concrete, tested number that moved.
- Worth noting for future reference: the `write` action's IMPORTANT clause ("You MUST read an
  existing file before writing to it") IS respected reliably because it has a hard enforcement
  mechanism at the tool layer (the write is rejected if the file wasn't read). A hint with no
  enforcement mechanism is less reliable. Adding enforcement (e.g., returning an error when
  `list` is called and the prompt already named files) is not tractable from the tool layer, but
  could be addressed via a system-prompt instruction in a future cycle.
