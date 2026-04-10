# 0001 — extra-tools-dead-call — results

- Issue: #2
- Branch: cicd/0001-extra-tools-dead-call
- PR: (opened in Phase 8)
- Date: 2026-04-10

## Metric

Command: `python3 agent.py --help 2>&1 | grep -c 'Failed to load extra tool'`

- Baseline: 1 (`/tmp/agent-cicd/probes/0001-startup-before.log`)
- After:    0 (`/tmp/agent-cicd/probes/0001-startup-after.log`)
- Delta:    −1 (−100%)

Secondary observations:

- `python3 -m unittest discover tests 2>&1 | grep -c 'Failed to load extra tool'` — 1 → 0 (the header warning that polluted every test run is gone).
- Every `.py` module in the builtin `tools/` package is now loaded exactly once at startup instead of twice.

## Test suite

- Before: 84 passing
- After:  86 passing (+2 new regression tests in `tests/test_load_extra_tools.py`)

## Probe re-run

- Before log: `/tmp/agent-cicd/probes/0001-startup-before.log`
- After log:  `/tmp/agent-cicd/probes/0001-startup-after.log`
- Verdict: PASS — warning gone, `--help` exits 0, test suite green.

## What I actually changed

- `.gitignore` — added `!plan/CICD/` / `!plan/CICD/**` overrides so the CICD loop's plans, results, and progress log are tracked in git even though the wider `plan/` tree stays ignored. Without this, the loop could not persist its own artifacts through a PR.
- `agent.py:152-158` — retargeted `_agent_tools_dir` from `CWD/tools` (same as the builtin package — root cause) to `CWD/.agent/tools`. Added a three-line comment explaining why pointing at the builtin package is unsafe.
- `tests/test_load_extra_tools.py` — new file, two tests:
  - `test_agent_help_emits_no_extra_tool_warning` runs `agent.py --help` as a subprocess from the repo root and asserts the warning is absent from combined stdout+stderr. Pins the integration-layer regression.
  - `test_load_extra_tools_registers_tool_from_temp_dir` exercises `tools.load_extra_tools` against a `tempfile.TemporaryDirectory` containing a minimal extra-tool file and asserts the tool lands in `MAP_FN` and `tools.tools` exactly once. Documents the helper's contract so the feature can't silently rot after the caller moved.
- `plan/CICD/agent.md`, `plan/CICD/improvements/0001-extra-tools-dead-call.md`, `plan/CICD/improvements/0001-extra-tools-dead-call.results.md`, `plan/CICD/progress.md` — tracked into the repo for the first time.

## What I learned

- The archive grep was a high-value move: it proved the caller was dead code left over from the `tool-agent/` + `e0/tools/` split, not an intentional overlay. That turned "should I delete or retarget?" into an easy call.
- First draft of the test plan had the subprocess running from a fresh tmpdir — which would have green-tested a broken state because the bug only reproduces when `CWD/tools` exists and is the builtin package. The gap-fill pass caught it. Rule of thumb for next cycle: any regression test that can go green under the old code is not a regression test.
- The repo's `.gitignore` ignored `plan/`, which directly blocked Phase 8 of the loop. I unignored `plan/CICD/` specifically. Future cycles should check `.gitignore` before assuming artifacts will commit.
