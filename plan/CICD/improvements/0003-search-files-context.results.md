# 0003 — search-files-context — results

- Issue: #5
- Branch: cicd/0003-search-files-context
- PR: (opened in Phase 8)
- Date: 2026-04-10

## Metric

**Primary**: tool-call count on the P-enum probe — "List every call site of `safe_cb` with file:line and hook name". Measured as:

```bash
grep -c '^INFO: TOOL CALL: ' <probe-log>
```

| | Before | After |
|---|---|---|
| Tool calls | 5 | **2** |
| Turns | 6 | 3 |
| Final answer | `TOTAL: 12` ✓ | `TOTAL: 12` ✓ |
| Sites listed correctly | 12/12 | 12/12 |
| `agent.py:66` hook_name | `-` | `-` |

- Baseline log: `/tmp/agent-cicd/probes/0003-penum-before.log`
- After log:   `/tmp/agent-cicd/probes/0003-penum-after2.log`
- Delta: **−3 tool calls (−60%)**, target was ≤ 2. PASS.

The after-run's two tool calls were:
1. `search_files({"pattern": "safe_cb\\("})` — now returns all 15 hits with 3 lines of surrounding context, so the match list already reveals which hit is the definition (`def safe_cb(...)`), which are calls, and which are doc/markdown mentions.
2. `think(...)` — internal pass to filter def/doc hits, classify each call's `hook_name`, and produce the final list. No file reads needed.

The before-run needed 5 tool calls because after the initial bare-match search the model had no context to disambiguate the 15 hits, so it fell back to reading three whole files (`agent.py`, `commands.py`, `tests/test_callbacks.py`) before a final think pass.

## Test suite

- Before: 94 passing
- After:  106 passing (+12 new)

New tests in `tests/test_search_files.py`:
- `test_context_zero_matches_legacy_shape` — pins the `context=0` byte-for-byte output, the backward-compat tripwire.
- `test_context_zero_two_files_no_separator`
- `test_no_matches_still_returns_header`
- `test_context_one_emits_before_and_after_lines`
- `test_context_clamps_at_file_boundaries`
- `test_negative_context_clamped_to_zero`
- `test_absurd_context_clamped_to_max` — verifies the `_MAX_CONTEXT = 20` cap.
- `test_context_merges_adjacent_windows` — two hits inside each other's context window produce one merged group, no `--`.
- `test_context_separates_disjoint_windows` — two hits far apart produce two groups with `--` between them.
- `test_context_separates_between_files`
- `test_definition_advertises_context_param` — pins the tool schema so the LLM can reach the parameter.
- `test_default_context_matches_definition` — pins the `fn` signature and the schema `default` in sync, so a drift in either alone trips a test.

## Probe re-run

- Before log: `/tmp/agent-cicd/probes/0003-penum-before.log`
- First after log (context parameter added, default still 0): `/tmp/agent-cicd/probes/0003-penum-after.log` — **4 tool calls, 5 turns, PASS on correctness but MISSED the tool-call target.** Model didn't autonomously pick up the new optional parameter.
- Second after log (default=3): `/tmp/agent-cicd/probes/0003-penum-after2.log` — **2 tool calls, 3 turns, PASS.** Target met.

## What I actually changed

- `tools/search_files.py` — added `context: int = 3` parameter to `fn` and to the tool schema. When `context == 0`, the code runs a short legacy-shape path that emits `path:line: text` one per match with no separators, byte-identical to today. When `context > 0`, it collects hit line numbers, expands each into a `[max(1,n-ctx), min(total,n+ctx)]` window, merges overlapping or touching windows per-file, and emits the merged windows with matched lines as `path:line: text` and context lines as `path-line- text` (grep-style `-` separator), with `--` between disjoint groups (same convention as `grep -C`, whether the gap is inside one file or across files). Added `_MAX_CONTEXT = 20` constant to bound the window size. Tightened the tool description so the model knows context removes the need for follow-up file reads, and bumped the default to 3 so even models that don't discover optional parameters benefit.
- `tests/test_search_files.py` — new file, 12 tests covering context=0 shape, context boundary cases, window merging, disjoint grouping, cross-file separators, negative/absurd values, and the schema-to-signature consistency pin.

## What I learned

- **Optional parameters don't move metrics unless the model reaches for them.** The first after-run (4 tool calls) was a classic "I added the feature, schema advertises it, surely the model will use it" mistake. The 31B-class model on this box didn't — it issued `search_files` with the old arg set and then followed up with full file reads, same as before. Changing the **default** was what moved the number. Lesson for future cycles: when a new tool parameter is obviously the right move 95% of the time, default it on instead of hoping the model finds it.
- **Keeping a legacy-shape tripwire was the right call.** I was briefly tempted to rip out the `context=0` branch once I switched the default; keeping it meant one named test still pins the exact historical shape, which is the cheapest possible insurance policy and cost nothing at runtime.
- **The first draft of the plan said "default 0 — preserves existing behaviour", and the plan's own risk section flagged that "the model doesn't know the new parameter exists".** The risk was real. I should have treated "model doesn't reach for an opt-in param on its own" as a near-certainty on this hardware and gone straight to default=3 from the start. The gap-fill pass should have caught this by asking, harder, what the model actually sees at turn 1.
- **Probes with a model in the loop are noisy, but metric thresholds are robust.** I didn't chase 6 → 5 → 4 → 3 → 2 across many reruns; one clean PASS on a meaningful threshold was enough. If the threshold had been "exact 2" instead of "≤ 2" I'd have been stuck re-running until variance favoured me, which is how fake wins happen.
