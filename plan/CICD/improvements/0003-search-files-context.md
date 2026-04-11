# 0003 — search-files-context

**Issue**: #5 — friction: search_files has no context lines, forcing full file reads to disambiguate hits
**Branch**: cicd/0003-search-files-context (will be created in Phase 6)

## Goal

Add grep-style context-lines support to `tools/search_files.py` so the agent can disambiguate hits (def vs call vs doc mention) in a single tool call instead of reading whole files afterwards.

## Motivation

Probe P-enum (this cycle, log `/tmp/agent-cicd/probes/0003-penum-before.log`) asked the agent to list every call site of `safe_cb`. The agent produced the correct answer (12 call sites) but needed **5 tool calls** and **6 turns** because `search_files` returned only bare match lines — no context — so the agent had to follow up with three full-file reads and a think pass. Issue #5 describes the symptom in full.

Adding a `context: int` parameter means the agent can see the surrounding code (enough to tell a definition line from a call from a markdown block) in the same result that the search already produces, and answer in one pass.

## Success metric

- **Primary**: number of `INFO: TOOL CALL:` lines in the P-enum probe log, measured as:
  ```bash
  grep -c '^INFO: TOOL CALL: ' /tmp/agent-cicd/probes/0003-penum-after.log
  ```
- **Baseline**: 5 (from `/tmp/agent-cicd/probes/0003-penum-before.log`)
- **Target**: ≤ 2 on the after-run. Going from "5 calls + 3 full reads" to "1 context-aware search + optional think" is the whole point of the change.
- **Gate**: after-run must also still PASS the probe — final line `TOTAL: 12` with the same 12 call sites listed.

Secondary (reported but not gating, because model behaviour is somewhat variable):
- Turn count from `grep -c '^INFO: --- Turn ' after.log` — expect 6 → ≤ 3.

## Scope

- **In**:
  - `tools/search_files.py` — add `context: int = 0` parameter, implement line buffering and output grouping.
  - `tests/test_search_files.py` — new file, unit tests for context=0 (current shape) and context>0 (grouped hits, overlap merging, edge cases).
- **Out**:
  - Glob exclusion, multiline regex, binary file handling — tempting but out of scope; file separate issues if they come up while implementing.
  - Any other tool in `tools/`.
  - `agent.py`, `commands.py`, `callbacks.py`, the TUI.
  - Changing the default behaviour when `context` is not passed — existing callers must see the same output byte-for-byte.

## Implementation steps

1. Read `tools/search_files.py` end-to-end (it's ~113 lines — small target, read once before touching).
2. Add `context: int = 0` parameter to `fn()`. Validate: clamp negatives to 0, clamp absurdly large values to e.g. 20 to bound memory.
3. Replace the per-file inner loop with a two-pass pattern: (a) read the file once into `lines: list[str]`; (b) collect matching line numbers (1-indexed) into `hit_lines: set[int]`; (c) for each hit expand into a window `[max(1, n-ctx), min(total, n+ctx)]`; (d) merge windows that overlap OR touch (i.e. `w1.hi >= w2.lo - 1`); (e) emit the merged windows in order, marking matched lines (line number in `hit_lines`) with `path:line: text` (unchanged shape) and non-match context lines with `path-line- text` (grep-style `-` separator) so the agent can visually distinguish matches from context at a glance.
4. Between disjoint groups — whether the gap is inside one file or crosses into the next file — emit a `--` separator line (matching `grep -C` convention). Do **not** emit `--` before the first group or after the last.
5. When `context == 0`, skip the windowing entirely and run the existing legacy-shape path (emit `path:line: text` directly, no `--` separators, no context markers) so the output is byte-identical to today. This protects every existing caller.
   - The `_MAX_RESULTS = 100` cap continues to count **match lines** only (not context lines), so a hit-heavy query doesn't get cut off just because context inflates the emitted line total. The header `[Searched … files, … matched, N results]` keeps `N = len(hit_lines across files)` for the same reason.
6. Update the tool `definition` JSON schema to advertise the new `context` parameter: type integer, minimum 0, default 0, description mentioning "lines of context before/after each hit (like `grep -C`)".
7. Update the tool `description` string in the definition to mention the new capability in one short sentence.
8. Add `tests/test_search_files.py` with the cases listed below. Run `python3 -m unittest tests.test_search_files` after each edit to stay green incrementally.
9. Re-run the full suite (`python3 -m unittest discover tests`) and make sure the 94 existing tests are still green.
10. Re-run the P-enum probe against the worktree's `agent.py` and capture `/tmp/agent-cicd/probes/0003-penum-after.log`.

## Test plan

### Existing tests that must stay green

All 94 tests in `tests/`. None of them currently import or exercise `search_files`, so the risk of a cross-test regression is low, but I must run `python3 -m unittest discover tests` and see `94 passed` (or `94 + new`) before declaring victory.

### New tests in `tests/test_search_files.py`

Each test uses `tempfile.TemporaryDirectory` to write a minimal fixture tree, calls `search_files.fn(...)`, and asserts on the returned string.

1. `test_context_zero_matches_legacy_shape` — create a 3-line file with a single hit on line 2. Call `fn(pattern, path, context=0)`. Assert the output body is exactly `path:2: <line>` (no `--`, no `-` context markers). **This pins backward compatibility — if anyone ever breaks the default shape, this test is the tripwire.**
2. `test_context_one_emits_before_and_after_lines` — file with 5 lines, hit on line 3. Call with `context=1`. Assert the output includes `path-2- ...`, `path:3: ...`, `path-4- ...` in order; no `--` (only one hit group).
3. `test_context_clamps_at_file_boundaries` — file with 3 lines, hit on line 1. Call with `context=5`. Assert no negative line numbers, window starts at line 1, ends at line 3.
4. `test_context_merges_adjacent_windows` — file with 10 lines, hits on lines 3 and 5, `context=2`. Assert single merged window lines 1..7, **no `--` separator between the two hits** (because windows overlap), both `:3:` and `:5:` match markers present, intermediate line 4 rendered as `-4-` context.
5. `test_context_separates_disjoint_windows` — file with 20 lines, hits on lines 3 and 15, `context=1`. Assert there is a `--` separator between the two window groups and each hit has its own `-2-`/`-4-` and `-14-`/`-16-` context.
6. `test_context_separates_between_files` — two files each with one hit, `context=1`. Assert `--` separator between the two files.
7. `test_negative_context_clamped_to_zero` — `context=-5` behaves identically to `context=0`.
8. `test_absurd_context_clamped_to_max` — `context=9999` on a 50-line file with 1 hit produces no more than `2*MAX_CTX + 1` content lines (plus the header). The cap (e.g. `MAX_CTX = 20`) lives as a module-level constant and is asserted.
9. `test_definition_advertises_context_param` — import `search_files`, read `search_files.definition`, assert `"context"` is a key under `parameters.properties` with `type == "integer"` and `default == 0`. Pins the tool schema so the agent can actually discover the parameter.
10. `test_no_matches_still_returns_header` — no hits, `context=2`. Output is `"No matches found."`-style, unchanged.

### Probe re-run

- Probe: **P-enum** — same prompt as before, against the worktree's `agent.py`.
- Before log: `/tmp/agent-cicd/probes/0003-penum-before.log` (already captured, 5 tool calls, 6 turns, PASS).
- After log:  `/tmp/agent-cicd/probes/0003-penum-after.log`.
- Expected delta: tool-call count drops from 5 to ≤ 2, final answer still `TOTAL: 12` with the same 12 call sites.
- Measurement commands:
  ```bash
  grep -c '^INFO: TOOL CALL: ' /tmp/agent-cicd/probes/0003-penum-after.log
  grep -c '^INFO: --- Turn '    /tmp/agent-cicd/probes/0003-penum-after.log
  grep 'TOTAL:'                  /tmp/agent-cicd/probes/0003-penum-after.log
  ```

## Risks & mitigations

- **Risk**: existing callers (anything already parsing `search_files` output line-by-line) break if I accidentally shift the `context=0` shape.
  - **Mitigation**: test 1 above pins the legacy shape byte-for-byte. I run it before touching anything else so the safety net exists first.
- **Risk**: Overlap merging logic is easy to get off-by-one on.
  - **Mitigation**: tests 4 and 5 cover both the "should merge" and "should not merge" boundary in the same direction (adjacent context=2 with hits 2 apart, then disjoint context=1 with hits 12 apart).
- **Risk**: the model doesn't know the new parameter exists and still issues the old call.
  - **Mitigation**: the tool `definition` JSON schema is what the model actually sees; advertising `context` there is what makes it reachable. Test 9 pins this.
- **Risk**: probe result is model-variable — it might still take 5 calls on the after-run for unrelated reasons.
  - **Mitigation**: the metric is a threshold (≤ 2), not an exact number. If it doesn't hit, I re-run up to 3 times before declaring null-result. If all three after-runs miss, I take the median — cycle fails honestly per the rules.
- **Risk**: agent chooses to ignore context and still reads whole files.
  - **Mitigation**: prompt phrasing already says nothing about "read files" — if the model still does it, that's a separate friction worth a later cycle (prompt / tool-description tuning), and I'll file an issue and declare this cycle null-result.

## Rollback

The change is additive: a new parameter with a default value that preserves existing behaviour. To roll back cleanly:

```bash
git checkout main -- tools/search_files.py
rm tests/test_search_files.py
```

No database, no config migration, no downstream consumer needs a flag flip.

## Closes

Closes #5
