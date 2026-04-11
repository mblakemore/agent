# 0009 — search-files-path-warning — results

- Issue: #17
- Branch: cicd/0009-search-files-path-warning
- PR: (pending — opened after results commit)
- Commit range: 11e994e..HEAD (description edit → regression test → plan + results + progress row)
- Date: 2026-04-11

## Metric

- **Tool-call count on P-enum probe from empty tempdir**
  - Baseline: **2** (`/tmp/agent-cicd/probes/0009-enum-before.log`, turn 1 wasted on `search_files(pattern='safe_cb')` with no `path=`, turn 2 retried with the absolute path)
  - After:    **1** (`/tmp/agent-cicd/probes/0009-enum-after.log`, turn 1 is `search_files(path='/mnt/droid/repos/agent', pattern='safe_cb')` — the new description nudged the model into passing `path` on the first call)
  - Delta:    **−1 (−50%)**

Secondary signals (not the gating metric, but consistent with the win):

- Turn count: 3 → 2
- Log line count: 55 → 47 (−14.5%)

## Test suite

- Before: 116 passing
- After:  117 passing (new `test_definition_warns_about_cwd_default` in `tests/test_search_files.py`)

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0009-enum-after.log`
- Verdict: **PASS** — 12 call sites returned, matching ground truth (`agent.py:63`, `commands.py:25/56/70/81/96/100/137`, `tests/test_callbacks.py:192/199/203/217`). The `callbacks.py:393` definition line is correctly excluded.

## What I actually changed

- `tools/search_files.py`:
  - Main `definition.function.description` gained an `IMPORTANT:` clause telling the model to always pass `path` explicitly when it knows the directory, and explaining that the default `'.'` is the process working directory, which in automation mode is usually an empty temp dir.
  - `path` parameter `description` was rewritten from the passive "Directory to search in (default: current directory)." to an active sentence that mirrors the warning and mentions automation + working directory.
- `tests/test_search_files.py`:
  - New `TestSearchFilesDefinition.test_definition_warns_about_cwd_default` — lowercases both description strings and asserts `"automation"` and `"working directory"` both appear in each. A future reword that drops either substring will flip the test red, forcing a conscious re-add.
- `plan/CICD/improvements/0009-search-files-path-warning.md` + `.results.md` + `plan/CICD/progress.md` row — the usual cycle paper trail.

No behavioral change to `fn()`, no new parameters, no default flips, no path-resolution changes. Description-only — same category as cycle 0004's file-write auto-mkdir advertise, which is the prior art that proved this model follows explicit tool-description nudges.

## What I learned

- **The cycle-0007 recovery hint and the cycle-0009 upfront warning are both worth keeping.** 0007 rescues the run after a mistake; 0009 prevents the mistake. Defense in depth — a stricter model or a shorter prompt would still benefit from 0007's hint if the warning was ever missed.
- **A single-sentence `IMPORTANT:` clause in the main tool description is enough to change behavior on this model** (gemma-4-31B through llama-server). Cycle 0004 showed it for `file` write; cycle 0009 shows it again for `search_files`. Generalizable pattern: when a tool has a dangerous or suboptimal default, add an `IMPORTANT:` clause at the end of the main description rather than burying the warning in a parameter description.
- **Grep-based source-text assertions are the right regression guard for description rots.** They're fast, stable, and they fail loudly when the warning disappears. Cycles 0005/0006/0009 have now each used this pattern successfully.
- **Secondary signals moved with the primary metric.** Turn count dropped 3 → 2 and log lines dropped 55 → 47 in lockstep with the tool-call count, which is exactly what you'd expect from eliminating a full round-trip. No wasted work, no hidden regression.
