# 0009 — search-files-path-warning

**Issue**: #17 — friction: search_files description doesn't warn that path='.' is usually wrong in automation mode
**Branch**: cicd/0009-search-files-path-warning

## Goal

Stop the agent from burning turn 1 on a `search_files` call with no `path=` against an empty temp cwd. Nudge the model at tool-description time so it picks the right directory on the first call instead of relying on cycle 0007's zero-files recovery hint.

## Motivation

Baseline P-enum probe (`/tmp/agent-cicd/probes/0009-enum-before.log`, 55 lines) — prompt was "List every call site of safe_cb in /mnt/droid/repos/agent as file:line, excluding the definition itself." The agent's first tool call was:

```
-> search_files(pattern='safe_cb')
```

No `path=`. The cwd was `/tmp/probe-0009-enum` (empty). Result: `[Searched '/tmp/probe-0009-enum' (0 files, 0 matched, 0 results)]` plus the cycle-0007 recovery hint. Turn 2 retried with `path='/mnt/droid/repos/agent'` and got the answer. Turn 3 printed the final list. **2 tool calls, 3 turns, correct answer.** One of those tool calls is pure waste that an upfront description warning can eliminate.

The tool's current description is passive:

- Main `description`: "Search file contents for a regex pattern (like grep). Searches recursively through a directory and returns each hit with surrounding context lines by default…"
- `path` parameter description: "Directory to search in (default: current directory)."

Neither sentence tells the model that in automation mode the cwd is usually a throwaway temp dir that has nothing to do with the repo the user asked about. The zero-files recovery hint (cycle 0007) fires *after* the wasted call. This cycle closes the gap before the call.

## Success metric

- **Baseline**: 2 tool calls on the P-enum probe from empty tempdir (`/tmp/agent-cicd/probes/0009-enum-before.log`, line 21 and line 29 — two `-> search_files(...)` events).
- **Target**: 1 tool call on the re-run. The first call must carry `path='/mnt/droid/repos/agent'` (or equivalent absolute path pointing at the repo).
- **Done-when**: tool-call count = 1, probe verdict still PASS (12 unique `safe_cb` call sites listed, matching ground truth), log shows a single `-> search_files(path=…` line.
- **Measurement method**:
  ```bash
  grep -c '^  -> search_files' /tmp/agent-cicd/probes/0009-enum-after.log
  ```
  Plus a manual check that the listed call sites match ground truth: `grep -rn 'safe_cb(' --include='*.py' /mnt/droid/repos/agent | grep -v 'callbacks.py:393'`.

## Scope

- **In**:
  - `tools/search_files.py` — tighten the `definition.function.description` and the `path` parameter `description` so both explicitly warn that `path='.'` is the process cwd, which in automation mode (`-a`) is usually an empty temp dir, and the caller should pass the directory they actually want to search (repo root, absolute path preferred).
  - `tests/test_search_files.py` — extend the existing test module with a source-text regression test that asserts both description strings mention automation / working directory, so the warning can't rot out silently.

- **Out**:
  - Any behavior change in `tools/search_files.py` `fn()` — no new parameters, no default flip, no auto-detection, no change to path resolution. Description-only.
  - Any change to the cycle-0007 zero-files recovery hint — it stays as a backstop.
  - Any change to other tools' descriptions (`file`, `exec_command`, `read_pdf`, etc.) — even if they share the same cwd-default shape, that is out of scope and filed separately if observed.
  - Any change to `_MAX_RESULTS`, `_MAX_CONTEXT`, the glob filter, the hidden-file skip, or the compact-limit behavior upstream.

## Implementation steps

1. Edit `tools/search_files.py`. In the `definition` dict:
   - Append to the main `description` string a short, concrete warning that reads roughly: "The default path='.' is the process working directory, which in automation mode is usually an empty temp dir — always pass `path` explicitly when you know the directory you want to search." Keep the total description length reasonable (existing description is ~6 lines; add ~2).
   - Update the `path` parameter `description` from "Directory to search in (default: current directory)." to something like: "Directory to search in. Default '.' is the process working directory; in automation mode (`-a`) this is usually an empty temp dir, so pass the absolute path to the directory you want to search whenever you know it."
2. Run `python3 -m unittest discover tests 2>&1 | tail -10` — must stay at 116 passing.
3. Extend `tests/test_search_files.py` with a new test class (or a new method on an existing class) `test_definition_warns_about_cwd_default` that:
   - Imports `tools.search_files` as a module.
   - Asserts the main `definition["function"]["description"]` contains both the substring "automation" and the substring "path" — so the warning is both present and specific.
   - Asserts the `path` parameter description contains "automation" AND "working directory" — same pattern as cycle 0005/0006's grep-based regression tests for the SHARED RUNTIME docstring cleanup.
   - Rationale for source-text assertion over behavioral test: the risk is that a future refactor rewords the description back to the passive shape and silently regresses the probe. A text-level guard catches that. A behavioral test requires spinning up the agent loop with an LLM, which is way too fragile and slow for the unit suite.
4. Re-run the full suite and confirm 117 passing (116 + 1 new).
5. Re-run the P-enum probe in a fresh temp dir against the worktree's `agent.py` and capture to `/tmp/agent-cicd/probes/0009-enum-after.log`.
6. Count tool calls and record the delta. If the metric hasn't moved (still 2), the description wording wasn't assertive enough — iterate wording once, re-run once. If after a second attempt the metric still hasn't moved, treat as a null result and comment on the issue.

## Test plan

- **Existing tests that must stay green**: all 116 under `tests/`. Particular attention to `tests/test_search_files.py` which already covers behavior and header format (cycle 0003 + 0007 landed tests here).
- **New tests**: one test in `tests/test_search_files.py` — `test_definition_warns_about_cwd_default`. Source-text assertion on the two description strings.
- **Probe re-run**: P-enum from empty tempdir against `/tmp/agent-cicd/0009-search-files-path-warning/agent.py`. Expected delta: tool-call count 2 → 1; probe verdict PASS; log line count should also drop (fewer tool results = fewer lines).

## Risks & mitigations

- **Risk**: LLM ignores the new warning and still calls `search_files(pattern=…)` without `path`. **Mitigation**: cycle 0004 proved this model follows explicit description nudges (P-impl 4 → 3 on file-write auto-mkdir advertise). If it ignores this one too, fall back to null-result path — do not force the metric.
- **Risk**: description gets so wordy the model gets confused about other parameters. **Mitigation**: keep the warning to one short, direct sentence each (main + param). No bullet lists, no markdown emphasis, no multi-paragraph prose.
- **Risk**: grep-based regression test is too strict — e.g., asserts on "automation" literally while a future reviewer prefers "auto mode". **Mitigation**: the two substrings I'll assert on ("automation" and "working directory" or "path") are the most natural vocabulary for this warning. If a future rewording drops both, the regression test will fail and the reviewer will consciously re-add the warning in whatever form they pick. That is the behavior we want.
- **Risk**: the probe is non-deterministic — sometimes the model picks the right path even with the old description. **Mitigation**: if the baseline re-runs with the old description come out 1-tool-call (not 2), the issue repro is stale and I should null-result the cycle rather than claim a fake win. Only fix if the 2-call baseline reproduces cleanly in this session.

## Rollback

`git checkout -- tools/search_files.py tests/test_search_files.py` on the worktree branch undoes everything. Description-only change, no schema shift, no runtime behavior change; revert is safe and local.

## Closes

Closes #17
