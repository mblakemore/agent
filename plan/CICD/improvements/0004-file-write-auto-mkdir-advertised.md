# 0004 — file-write-auto-mkdir-advertised

**Issue**: #7 — friction: file write silently auto-creates parent dirs but the tool description doesn't say so, so models burn a turn on `mkdir -p` first
**Branch**: `cicd/0004-file-write-auto-mkdir-advertised` (created in Phase 6)

## Goal

Teach the LLM, via the `file` tool description, that `write` already creates parent directories, so it stops wasting a turn on `mkdir -p <dir>` before writing into a new directory.

## Motivation

P-impl probe (word_freq.py + tests/test_word_freq.py + run unittest) ran in `/tmp/probe-0004` against HEAD:

- **Tool calls**: 4 (`file write`, `exec_command mkdir`, `file write`, `exec_command unittest`)
- **Turns**: 5 (final turn is the text answer)
- **Wall time**: ~47s (00:08:57 → 00:09:44, from session log)
- **Verdict**: PASS (all 5 tests pass)

Probe log: `/tmp/agent-cicd/probes/0004-pimpl-before.log`
Session log snippet: `/tmp/probe-0004/.agent/history/session_20260411_000834.log`

The middle `exec_command({"command": "mkdir -p tests"})` call is pure waste. `tools/file.py:165` already runs `p.parent.mkdir(parents=True, exist_ok=True)` on every full-file write, but the tool description (`tools/file.py:250-261`) never mentions it. The model falls back to its LLM prior ("mkdir before writing into a new directory") and burns a whole turn.

Cycle 0003's results.md captured the exact lesson this cycle will test:

> Optional parameters don't move metrics unless the model reaches for them. […] Lesson for future cycles: when a new tool parameter is obviously the right move 95% of the time, default it on instead of hoping the model finds it.

Same shape here: the *behavior* is already correct (auto-mkdir on write) — the *surface area the model sees* is what's out of date. The plan is to close that gap in the description and prove the metric moves.

## Success metric

Two pinned metrics on the P-impl probe, both measured from the after-run probe log:

- **Primary**: tool-call count on P-impl.
  - Baseline: `4`
  - Target: `≤ 3` (drop the wasted `mkdir -p tests` call)
  - Measurement: `grep -c '^INFO: TOOL CALL: ' /tmp/agent-cicd/probes/0004-pimpl-after.log`
  - Verification: inspect the grep output — the three surviving calls must be two `file write`s (module + test) and one `exec_command` (run unittest). A 3-call run that skips the `unittest` exec is a PARTIAL, not a PASS.

- **Secondary**: turn count on P-impl.
  - Baseline: `5`
  - Target: `≤ 4`
  - Measurement: `grep -c '^INFO: --- Turn ' /tmp/agent-cicd/probes/0004-pimpl-after.log`

- **Correctness gate** (must hold or the whole cycle is null-result):
  - After-run leaves `word_freq.py` and `tests/test_word_freq.py` on disk in `/tmp/probe-0004-after/`.
  - `python3 -m unittest tests/test_word_freq.py` in that dir exits 0 with 5 passing tests (matched by `grep -E 'Ran 5 tests|OK$' /tmp/agent-cicd/probes/0004-pimpl-after.log`).

## Scope

- **In**:
  - `tools/file.py` — update the `write` line of the tool `description` to advertise auto-mkdir and explicitly instruct the model **not** to `mkdir` / `exec_command mkdir` before writing. Also add a matching hint to the `IMPORTANT:` paragraph if it improves signal without exceeding the description's current length by more than ~200 chars.
  - `tests/test_file_tool.py` — **new file** (no existing test module imports `tools.file` directly). Contains the two new tests described below. Uses the same header boilerplate as `tests/test_search_files.py` (sys.path insert + `from tools import file`).
- **Out**:
  - No change to the actual write logic at `tools/file.py:165`.
  - No change to the `append` / `insert` / `read` / `list` / `delete` paths.
  - No change to `exec_command.py`.
  - No change to the `/tools` slash command or README (cycle 0002 + the stale README line are separate work).
  - No change to default parameters of any other tool.

## Implementation steps

1. Read `tools/file.py` and its existing tests (`tests/test_file_*.py`) to find where `file` behavior is already pinned, so the new test lives next to its siblings.
2. Edit `tools/file.py` `description` field:
   - `write` line becomes: `"write: Create/overwrite a file. Parent directories are created automatically — do NOT call mkdir or exec_command before writing a file into a new directory. For line-range replacement, set both start_line AND end_line."`
   - Keep the rest of the description untouched.
3. Create `tests/test_file_tool.py` with:
   - `test_write_description_advertises_auto_mkdir`: reads `file.definition["function"]["description"]` and asserts it contains both `"Parent directories are created automatically"` and `"do NOT call mkdir"` as substrings (case-sensitive, short enough to survive minor rephrases).
   - `test_write_creates_missing_parent_dirs`: uses `tempfile.TemporaryDirectory()` + an **absolute** target path `<tmp>/a/b/hello.txt` (two new dirs — safely under `_MAX_NEW_DIRS = 3`), calls `file.fn("write", path=..., content="hi")`, then asserts (a) the returned string starts with `"Wrote '"`, (b) the file exists on disk with content `"hi"`, (c) both `<tmp>/a` and `<tmp>/a/b` exist and are directories. Absolute paths keep the test independent of the test runner's cwd and of the `_accessed_files` session cache.
4. Run the full test suite in the worktree. Expect 106 → 108 passing.
5. Re-run the probe **against the worktree's `agent.py`** in a fresh working directory. The after-dir must be created with `rm -rf /tmp/probe-0004-after && mkdir -p /tmp/probe-0004-after && cd /tmp/probe-0004-after` (matching the baseline setup — no stale `word_freq.py`, no stale `.agent/` bash-session state from an earlier probe). Log to `/tmp/agent-cicd/probes/0004-pimpl-after.log`. Also capture wall time by diffing the first and last `HH:MM:SS` in the session log.
6. If the after-run is already PASS on the metric, write results.md and move to TRACK.
7. If the after-run still shows a `mkdir` call, tighten the description — e.g. add the hint to the top-level `IMPORTANT:` paragraph as a second signal — and re-probe once. Stop after 3 debug iterations (hard rule).

## Test plan

- **Must stay green**: every existing test in `tests/` (106 passing on HEAD). Any red means I either fix the behavior or update the test in the same commit with a one-line justification in this plan's Implementation steps.
- **New tests**:
  - `test_write_description_advertises_auto_mkdir` — pins the wording in `tools/file.py`'s `description`.
  - `test_write_creates_missing_parent_dirs` — pins the actual auto-mkdir behavior on a nested path.
- **Probe re-run**: P-impl probe (exact prompt above) in `/tmp/probe-0004-after/` against the worktree's `agent.py`. Expected deltas: tool-call count `4 → ≤3`, turn count `5 → ≤4`, correctness unchanged (5 tests pass).

## Risks & mitigations

- **Risk: the model still `mkdir`s out of habit despite the description update.** This was exactly cycle 0003's first after-run symptom. Mitigation: use a *directive* phrasing ("do NOT call mkdir") rather than a *factual* one ("parent dirs are created automatically"). If the first after-run still mkdirs, add a second mention in the `IMPORTANT:` paragraph and re-probe. If it still mkdirs after that, null-result the cycle honestly.
- **Risk: the description grows long enough to push other tool descriptions out of the model's attention.** Mitigation: the added phrase is <200 chars; overall description stays under ~900 chars. Also, the fix only touches the `write` sub-line, not the top-level summary.
- **Risk: the wording-tripwire test becomes fragile if we ever want to rephrase.** Mitigation: assert on two short semantic substrings (`"Parent directories are created automatically"` and `"do NOT call mkdir"`), not the whole paragraph, so a rephrase that preserves the meaning still passes.
- **Risk: a future `file.py` refactor drops auto-mkdir and the description lies.** Mitigation: `test_write_creates_missing_parent_dirs` asserts the *behavior*, so the lie trips an actual test, not just a code-review smell.
- **Risk: single-run probe noise (model decides to write a requirements.txt, or wrap something extra).** Mitigation: the correctness gate accepts any 3-call layout that includes 2 writes + 1 unittest exec; anything else is a PARTIAL and the cycle keeps debugging.

## Rollback

- `git checkout main -- tools/file.py tests/test_file_*` (or delete the new test file if it was fresh) inside the worktree, then `python3 -m unittest discover tests` to confirm green. Worktree branch can be deleted via the null-result path in the main agent.md Phase 8.

## Closes

Closes #7
