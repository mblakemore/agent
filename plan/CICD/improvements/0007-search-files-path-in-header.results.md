# 0007 — search-files-path-in-header — results

- Issue: #13
- Branch: cicd/0007-search-files-path-in-header
- PR: (opened in Phase 8)
- Commit range: b1e7738..(final)
- Date: 2026-04-11

## Metric

### Primary (robust, code-level) — PASS

`tests/test_search_files.py` gained a new `TestSearchFilesHeaderIdentity` class with **4 new tests** pinning the header shape, the partition contract, and the zero-files hint body:

- `test_header_names_resolved_absolute_path_on_hit`
- `test_header_names_resolved_absolute_path_on_miss_with_files`
- `test_zero_files_emits_hint_line`
- `test_header_shape_body_partition_still_works`

Measurement:

```bash
cd /tmp/agent-cicd/0007-search-files-path-in-header
python3 -m unittest discover tests 2>&1 | tail -3
# Ran 114 tests in 1.873s
# OK
```

| | Before | After |
|---|---|---|
| Total tests | 110 | **114** |
| search_files tests | 12 | **16** |
| Header-identity tests | 0 | **4** |

### Secondary (probe-level) — PASS

P-enum probe ("list every `safe_cb` call site") run from `/tmp/probe-0007-enum` — a fresh empty temp dir, the cwd the agent lands in before any `path=` disambiguation.

| Metric | Before | After (run 1) | After (run 2) |
|---|---|---|---|
| Tool calls | **4** | 1 | **2** |
| Session messages | 10 | 4 | 6 |
| Verdict vs target (≤ 3) | — | PASS | PASS |

- Baseline log: `/tmp/agent-cicd/probes/0007-enum-before.log`
- After run 1:  `/tmp/agent-cicd/probes/0007-enum-after.log` (1 tool call — model passed `path=` on first attempt; lucky variance, fix never fired)
- After run 2:  `/tmp/agent-cicd/probes/0007-enum-after2.log` (2 tool calls — **fix fires exactly as designed**)

Run 2 is the honest demonstration. The first `search_files` call returned:

```
[Searched '/tmp/probe-0007-enum' (0 files, 0 matched, 0 results)]
No files were searched under '/tmp/probe-0007-enum'. If you meant a different directory, pass path= with an absolute path.
```

…and the model's **second** call was the same regex pattern with `"path": "/mnt/droid/repos/agent"` added, yielding 12 results in one shot. The wasted second `search_files` and the `exec_command find` fallback from the baseline run are both gone.

Net delta: **4 → 2 tool calls (−50%)** on the run that actually exercised the fix; **4 → 1 (−75%)** on the run where the model got lucky and didn't need the hint. Both comfortably under the ≤ 3 target.

## Test suite

- Before: 110 passing
- After:  114 passing (+4 new)

## Probe re-run

- Baseline log: `/tmp/agent-cicd/probes/0007-enum-before.log`
- After logs:   `/tmp/agent-cicd/probes/0007-enum-after.log`, `/tmp/agent-cicd/probes/0007-enum-after2.log`
- Verdict: **PASS** — both the unit-test pin and the probe-level signal cleared their thresholds, and run 2 shows the hint visibly shaping the model's second call.

## What I actually changed

- `tools/search_files.py` — after the `search_path.exists()` guard, compute `resolved = search_path.resolve()` (fall back to `.absolute()` on the unlikely `OSError`/`RuntimeError`). The header is now `[Searched '<abs-path>' (N files, M matched, K results)]`, preserving the `)]\n` terminator so cycle 0003's `_body()` partition helper still works. The truncation marker slots between `)` and `]`. When `total_matches == 0` **and** `files_searched == 0`, the body is the explicit hint: `No files were searched under '<abs-path>'. If you meant a different directory, pass path= with an absolute path.`; when `files_searched > 0` the body stays the byte-identical `No matches found.` so the "right path, wrong pattern" case stays quiet.
- `tests/test_search_files.py` — new `TestSearchFilesHeaderIdentity` class with four tests. The partition-contract tripwire walks all three header shapes (hit, miss-with-files, zero-files) to catch any future header tweak that would silently break cycle 0003's `_body()` helper.

## What I learned

- **Cycle 0003's "optional parameters don't move metrics" lesson generalizes to *error messages*.** The fix doesn't give the model a new tool or a new argument — it gives it a clearer failure mode. A hint that's part of every 0-files response (not an opt-in, not a debug mode) is what turned the feedback loop from "retry until shell fallback" into "oh, wrong dir, fix it". Same lever as cycle 0003 pulled, different surface.
- **LLM-driven probe metrics can flatter you by accident.** Run 1 had 1 tool call because the model randomly chose to pass `path=` up front — the fix never fired. If I'd stopped there I'd have recorded a clean −75% win that wasn't caused by the code change at all. Run 2 cost almost nothing and gave me the honest demonstration. Lesson: when a probe's cleanest reading happens on the run that *didn't exercise the code path you changed*, do the second run.
- **Keeping the miss-with-files body byte-identical was worth the extra branch.** The cycle 0003 tests assert `body == "a.txt:2: beta"` on an exact-match shape; splitting the zero-body into two cases lets me add the hint to the one that needs it without touching the one that doesn't. Cheaper than retrofitting those tests to accept both shapes.
- **The `)]\n` terminator discipline is doing real work.** The partition-contract tripwire test exists precisely so the next cycle that wants to tweak this header has to consciously opt out of the cycle 0003 helper contract. If I'd just said "the new shape still ends in `]\n`, trust me" a future refactor could silently break it.
