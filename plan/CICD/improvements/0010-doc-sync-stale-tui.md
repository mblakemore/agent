# 0010 — doc-sync-stale-tui

**Issue**: #19 — friction: README and tui.py still reference a `--tui` flag that no longer exists, and `/tools` docs say "last 20" after paging shipped
**Branch**: cicd/0010-doc-sync-stale-tui

## Goal

Eliminate the 6 stale doc-as-code references in `README.md`, `tui.py`, and `agent.py` that describe a CLI shape (`--tui` flag, `/tools` default of "last 20") the agent no longer has, and lock the fix in with a grep-based regression test so the drift can't silently re-appear.

## Motivation

Discovered during cycle 0010 Phase 1 (PERCEIVE), while re-reading `README.md` to refresh the feature surface. Five refs name a `--tui` flag that was removed in commit `008d84a` (TUI-default), and one ref says `/tools` shows "the last 20 tool calls" — cycle 0002 (PR #1) shipped paging with a 50-entry buffer and `/tools [N|all]` default of "all". All six are user-visible (README, module docstring, an `ImportError` message, one code comment).

- Issue: #19 (full impact writeup)
- Probe logs (for PERCEIVE grounding — no PROBE re-run needed, this is a doc-layer cycle):
  - `/tmp/agent-cicd/probes/0010-pcount-before.log` — P-count, 3 turns / 2 tool calls, PASS (ground-truth unchanged)
  - `/tmp/agent-cicd/probes/0010-penum-before.log` — P-enum, 2 turns / 1 tool call, PASS (cycle 0009 result holds)

This cycle is the same shape as cycles 0005 / 0006 / 0009: a targeted text edit plus a grep-based regression test. Prior art says the pattern lands cleanly.

## Success metric

- **Baseline**: `6` stale refs
  - `grep -c -- '--tui\b' README.md tui.py agent.py` → 2 + 2 + 1 = 5
  - `grep -c 'last 20 tool calls' README.md` → 1
  - Sum: **6** (captured 2026-04-11 at HEAD `93636a1`)
- **Target**: `0` — both greps return empty
- **Measurement method**:

  ```bash
  cd /tmp/agent-cicd/0010-doc-sync-stale-tui
  { grep -c -- '--tui\b' README.md tui.py agent.py; \
    grep -c 'last 20 tool calls' README.md; } \
    | awk -F: '{s+=$NF} END {print s}'
  ```

  A successful cycle prints `0`. The grep-based regression test added in step 2 below fails loudly if anyone re-introduces either phrase.

## Scope

- **In**:
  - `README.md` — lines 61, 93, 149: rewrite `/tools` row, project-layout `tui.py` comment, optional-deps bullet.
  - `tui.py` — lines 5 (docstring), 325 (`_INSTALL_HINT` string).
  - `agent.py` — line 1278: update the stale `--tui`-by-default comment.
  - `tests/test_doc_sync.py` — new file, one `unittest.TestCase` with two assertions (no `--tui\b` across the three files; no "last 20 tool calls" in `README.md`).
- **Out**:
  - Any behavioral change to the TUI, `/tools`, or argparse — this is pure text hygiene.
  - Rewriting unrelated README sections (configuration, colors, how-it-works) — leave untouched to keep the diff tight and the review cheap.
  - Adding a broader doc-as-code linter that parses `argparse` and cross-checks the README flag table — tempting, but scope creep; leave as a candidate for a future cycle.

## Implementation steps

1. **Fix `README.md:61`** — rewrite the `/tools` row to describe the real shape: default `/tools` shows all buffered calls (up to 50 in the default buffer); `/tools N` shows the last `N`; `/tools all` is an explicit synonym for the default.
2. **Fix `README.md:93`** — rewrite the `tui.py` project-layout comment: drop `(--tui)`, replace with `(default prompt; --no-tui to disable)` or similar short active phrasing.
3. **Fix `README.md:149`** — rewrite the optional-deps bullet to say `prompt_toolkit` is on by default in interactive mode and is optional in the sense that the agent falls back automatically if it isn't installed (matches actual behavior in `agent.py:1295-1298`).
4. **Fix `tui.py:5`** — rewrite the module docstring's stale sentence. The module is imported whenever interactive mode runs (TUI is the default); the `_AVAILABLE` flag and `_IMPORT_ERROR` stubs handle the missing-package case. Drop the `--tui` mention entirely.
5. **Fix `tui.py:325`** — rewrite `_INSTALL_HINT`. This is a user-facing error message raised only if a caller instantiates `TuiSession()` directly while `prompt_toolkit` is missing. The text should describe the condition accurately ("interactive TUI mode requires `prompt_toolkit`; install it or pass `--no-tui`") rather than naming a nonexistent flag.
6. **Fix `agent.py:1278`** — rewrite the comment. TUI-by-default is a behavior, not a flag; the comment should explain *why* the fallback exists (so environments without `prompt_toolkit` still get an interactive prompt) without naming a dead flag.
7. **Add `tests/test_doc_sync.py`** — one `TestCase` with two tests:
   - `test_no_stale_tui_flag`: read each of `README.md`, `tui.py`, `agent.py`; assert that `re.search(r'--tui\b', text)` is `None` for each. Failure message names the offending file and the first matching line for a clean debug.
   - `test_tools_command_docs_do_not_claim_last_20`: read `README.md`; assert that the literal substring `'last 20 tool calls'` is not present. Failure message is a one-liner pointing at the `/tools` row.
   - Rationale for two tests (not one big one): each test covers one drift mode and fails independently, so a future regression lights up precisely the right red.
8. **Run the grep measurement command** from "Measurement method" above inside the worktree — expect `0` — and append the raw output to the results file in Phase 8.
9. **Run the full test suite** — expect `117 → 119` (both new tests pass, nothing else regresses).

## Test plan

- **Existing tests that must stay green**: all 117 tests under `tests/` — especially `tests/test_tui.py` (28 tests, touches the TUI module), `tests/test_commands.py` (13 tests, touches `/tools`), `tests/test_tools_paging.py` (7 tests, direct coverage of the `/tools` paging feature).
- **New tests I'll add**:
  - `tests/test_doc_sync.py::TestDocSync::test_no_stale_tui_flag` — grep-based, asserts zero `--tui\b` matches across `README.md`, `tui.py`, `agent.py`.
  - `tests/test_doc_sync.py::TestDocSync::test_tools_command_docs_do_not_claim_last_20` — grep-based, asserts `'last 20 tool calls'` is absent from `README.md`.
- **Re-run probe**: none required. This cycle does not change runtime behavior, so the probe metrics from cycles 0007/0008/0009 all carry over. I will still re-run the existing P-count probe once in Phase 7 as a sanity check that the tests directory is still clean.

## Risks & mitigations

- **Risk**: the new test file's regex is stricter than the intent (e.g., catches a hypothetical future `--tui-colors` flag that is legitimately different from the removed `--tui`).
  - **Mitigation**: use `\b` (word boundary) at the end of the regex so `--tui-colors` wouldn't match. Current regex `--tui\b` explicitly allows `--tui-foo` style future flags.
- **Risk**: someone later legitimately re-introduces a `--tui` flag (e.g., the TUI becomes opt-in again).
  - **Mitigation**: the test is a one-line regex assertion. A future cycle that re-introduces the flag deletes the assertion in the same commit as the flag addition — same cost as adding the test. Grep-based regression guards are cheap to revoke.
- **Risk**: rewriting the `_INSTALL_HINT` error message changes a string some downstream tool grepped for.
  - **Mitigation**: searched the repo for consumers of `_INSTALL_HINT`; only `TuiSession.__init__` reads it, as a one-shot `ImportError` message. No tests match on the exact text today, so the rewrite is safe.
- **Risk**: README rewrite accidentally breaks markdown table rendering.
  - **Mitigation**: keep column count identical (pipe-delimited, same number of pipes). I'll read the rendered output mentally before committing. Not shipping a full markdown linter for one cycle.

## Rollback

Every change is additive or a text replacement on a single line. Rollback = `git revert` the 3 commits on `cicd/0010-doc-sync-stale-tui` (one per logical step: doc edits, test file, plan/results/progress). No schema migrations, no persisted state, no new dependencies.

## Closes

Closes #19
