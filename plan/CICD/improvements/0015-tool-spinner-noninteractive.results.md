# 0015 — tool-spinner-noninteractive — results

- Issue: #34
- Branch: cicd/0015-tool-spinner-noninteractive
- PR: #(pending — draft)
- Commit range: (single commit range on the branch)
- Date: 2026-04-11

## Metric

- **Baseline** (`grep -cE '^  -> \w+ +-> \w+\(' /tmp/agent-cicd/probes/0015-pcount-before.log`): **1**
- **After** (`grep -cE '^  -> \w+ +-> \w+\(' /tmp/agent-cicd/probes/0015-pcount-after.log`): **0**
- **Delta**: −1 (−100%)
- **Sanity gate**: `grep -cE '^  -> \w+\(' /tmp/agent-cicd/probes/0015-pcount-after.log` = **1** — the after-run still printed its tool-start line, proving the tool loop fired and the fix removed duplication rather than removing the render entirely.

### Before (line 20 of `0015-pcount-before.log`)

```
  -> exec_command   -> exec_command(command='grep -r "def test_" /mnt/droid/repos/agent/tests/…)
```

### After (line 20 of `0015-pcount-after.log`)

```
  -> exec_command(command='grep -r "def test_" /mnt/droid/repos/agent/tests/…)
```

## Test suite

- Before: 122 passing
- After: 123 passing (+1 regression test: `tests/test_spinner.py::TestSpinnerInteractivity::test_agent_tool_spinner_gated_on_interactive`)

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0015-pcount-after.log`
- Verdict: **PASS** — 2 turns, 1 tool call, correct answer ("122"), clean tool-start render, zero duplication.

## What I actually changed

- `agent.py:1785` — extended the `use_spinner` predicate with `and not theme._no_color()` so the per-tool `StreamStatus` is skipped entirely in non-interactive mode. The `on_tool_start` callback already prints the full-form line at `callbacks.py:280`, so suppressing the redundant prefix write leaves the log strictly cleaner with no information loss.
- `tests/test_spinner.py` — added `test_agent_tool_spinner_gated_on_interactive` that reads `agent.py` source and asserts the gate via a whitespace-tolerant regex. Future refactors that drop the gate fail this test loudly.
- `plan/CICD/improvements/0015-tool-spinner-noninteractive.md` — the cycle plan (this cycle's plan file).
- `plan/CICD/improvements/0015-tool-spinner-noninteractive.results.md` — this file.
- `plan/CICD/progress.md` — appended cycle 0015 row.

## What I learned

- Cycle 0014's "out of scope" follow-up sightings are a useful source of probe-confirmed issues for the next cycle. Issue #34 came straight out of cycle 0014's scope notes plus a fresh P-count run that reproduced the symptom on line 1. The cost of running a cheap probe and cross-referencing prior "worth filing as a separate issue" lines is nearly zero and produces actionable targets.
- The `on_tool_start` callback was designed with TTY `CLEAR_LINE` semantics in mind and works correctly for interactive users; the bug only shows up in captured logs. That's why cycle 0011's P-bug probe logs were noisy in a way I didn't notice at the time — half the "per-call" width was the duplication. Future cycles that care about P-bug tool-call *visual* readability just got cleaner baselines for free.
- Call-site gating vs. library gating: fixing this in `spinner.py` would have broken the assistant-streaming header in non-interactive mode (`agent.py:1558-1559` relies on the non-interactive prefix write). The correct surface for a "one caller doesn't need this behavior" change is the caller, not the shared library. Generalizable rule: when only one of a library's N callers wants the feature off, gate at the caller.
