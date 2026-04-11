# 0010 — doc-sync-stale-tui — results

- Issue: #19
- Branch: cicd/0010-doc-sync-stale-tui
- PR: (pending — opened after this commit lands)
- Commit range: `d84f92b..HEAD` (doc edits → regression test → plan + results + progress row)
- Date: 2026-04-11

## Metric

- **Count of stale doc-as-code refs in `README.md`, `tui.py`, `agent.py`**
  - Measurement: `{ grep -c -- '--tui\b' README.md tui.py agent.py; grep -c 'last 20 tool calls' README.md; } | awk -F: '{s+=$NF} END {print s}'`
  - Baseline: **6** (README.md:93, README.md:149, tui.py:5, tui.py:325→328, agent.py:1278, README.md:61)
  - After:    **0** (both greps return empty; awk sum prints `0`)
  - Delta:    **−6 (−100%)**

Secondary signals (not the gating metric):

- `git diff --stat d84f92b^..HEAD` on main code: `README.md` 3 lines, `tui.py` 17 lines (docstring expanded from 4 → 10 lines to explain the `_AVAILABLE` fallback properly, plus 4-line `_INSTALL_HINT` rewrite), `agent.py` 2 lines. No new imports, no new symbols, no behavior edits.
- New test file `tests/test_doc_sync.py`: 48 lines, two test methods, zero external dependencies.

## Test suite

- Before: 117 passing (`python3 -m unittest discover tests` at HEAD `93636a1`)
- After:  119 passing (117 existing + `TestDocSync.test_no_stale_tui_flag` + `TestDocSync.test_tools_command_docs_do_not_claim_last_20`)

No existing tests changed. The `tests/test_tui.py` (28), `tests/test_commands.py` (13), and `tests/test_tools_paging.py` (7) suites — the ones most likely to break on TUI or `/tools` drift — all stay green.

## Probe re-run

None required. This cycle does not change runtime behavior (no tool calls differ, no tool descriptions changed, no loop code touched outside a comment). Cycle 0009's P-enum and cycle 0002/0003's P-count baselines carry over unchanged. For extra safety I re-ran P-count and P-enum at baseline during Phase 2 — both PASS with the same shape as after cycle 0009:

- `/tmp/agent-cicd/probes/0010-pcount-before.log` — P-count (117 total, 2 tool calls, 3 turns), PASS
- `/tmp/agent-cicd/probes/0010-penum-before.log`  — P-enum (12 call sites correctly excluded `callbacks.py:393`, 1 tool call, 2 turns), PASS

## What I actually changed

- `README.md:61` — `/tools` commands-table row rewritten to describe the real shape: `/tools [N|all]` with default `all` over a 50-entry buffer. The `|` inside the cell is escaped (`\|`) so the 2-column markdown table stays valid.
- `README.md:93` — project-layout comment for `tui.py` now says `prompt_toolkit front-end (default in interactive mode; --no-tui to disable)`.
- `README.md:149` — optional-deps bullet for `prompt_toolkit` rewritten to explain it's *used by default* but technically optional (the agent falls back to `input()` automatically if missing), and mentions `--no-tui` for the force-plain-prompt case. Keeps the "optional" framing because the agent does still work without it.
- `tui.py:1-11` — module docstring rewritten. The previous version said `--tui is passed but the package is missing`; the new version describes the `_AVAILABLE` module-level flag, the `main()` fallback path, and the direct-instantiation `ImportError` case from the stub class — all of which are the actual behavior.
- `tui.py:328-332` — `_INSTALL_HINT` rewritten from `"--tui requires the optional prompt_toolkit package..."` to `"Interactive TUI mode requires the optional prompt_toolkit package. Install it with pip install prompt_toolkit, or pass --no-tui to use the plain input() prompt instead."`. Same error class (`ImportError`), same raise sites (`TuiSession.__init__` and the `else` branch stub), but a user who hits the error now reads a flag that actually exists.
- `agent.py:1274-1279` — in-code comment at the TUI instantiation site rewritten. Header changed from `── TUI front-end (optional) ──` to `── TUI front-end (default in interactive mode) ──`, and the `"--tui"-by-default doesn't break…` line rewritten to `"the default TUI path doesn't break…"`. Code around the comment (the `tui_session = None` / `if tui and not auto` block) is unchanged.
- `tests/test_doc_sync.py` — new 48-line test file with two grep-based assertions. Follows the exact pattern from `tests/test_tools_docstrings.py` (cycle 0005/0006) but scoped to the three text files that carried the drift.
- `plan/CICD/improvements/0010-doc-sync-stale-tui.md` + `.results.md` + `plan/CICD/progress.md` row — the usual cycle paper trail.

## What I learned

- **Doc-as-code drift compounds silently across even small repos.** Five refs to a flag that was removed 8 cycles ago survived every subsequent touch, including the `TuiCallbacks` work in phases A/B of `ui-upgrade-followup.md`. The reason is obvious in hindsight: none of the existing tests asserted anything about README or docstring content, so the reviewer's only check was visual, and visual checks miss stale flag names in long files. A one-line regex test fixes that class of problem permanently for a few seconds of LLM time.
- **`--tui`-style dead flags are a particularly sneaky drift mode because they're not broken functionality — they're instructions that tell users to type something the CLI rejects.** A user who follows `README.md:149` literally types `python3 agent.py --tui` at the shell and gets `unrecognized arguments: --tui`. Nothing is "broken" in the test sense, but the user's first interaction with the project is a failure. That's worth a dedicated regression guard.
- **The grep-based-source-text pattern is now a stable CICD idiom.** Cycles 0005, 0006, 0009, and 0010 have all used variants of it: walk a fixed set of files, search for a forbidden phrase, assert zero matches, include file:line in the failure message. It's fast (< 50ms per test), deterministic, and fails loudly — exactly the properties the loop needs for a guardrail that has to survive future cycles touching the same files. Worth formalizing into a tiny helper in a future cycle if a fifth use case appears.
- **The `_INSTALL_HINT` rewrite is the one edit a user is most likely to actually see.** It raises only when `prompt_toolkit` is missing AND a caller instantiates `TuiSession` directly without going through the `main()` fallback. That's a narrow window, but within it, the error message is all the user has. Previous version named a flag that doesn't exist; new version names `--no-tui` (which does exist) and `pip install prompt_toolkit`. Small, high-leverage fix.
