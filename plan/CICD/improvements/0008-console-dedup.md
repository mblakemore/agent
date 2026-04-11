# 0008 — console-dedup

**Issue**: #15 — friction: tool call/result and assistant text render twice on the console, bloating every session log
**Branch**: cicd/0008-console-dedup (will be created in Phase 6)

## Goal

Stop the console from rendering every tool call, tool result, and assistant message twice. The `ConsoleCallback` is the user-facing renderer; `log.info` copies of the same events are dead weight on stdout.

## Motivation

In the cycle-0008 P-bug baseline probe (`/tmp/agent-cicd/probes/0008-bug-before.log`, 206 lines), every tool call and every tool result appears back-to-back twice:

- Line 23: `TOOL CALL: file(...)` (log.info)
- Line 24: `-> file(action='list', path='.')` (callback)
- Lines 25–32: `TOOL RESULT [file]: …` untruncated (log.info)
- Lines 29–32: `    Result: …` truncated (callback)

And the same for the `ASSISTANT:` / streamed `Assistant:` text (lines 186–204) and `USER:` / `You:` input echo (lines 15–16). The log-handler copy is untruncated, so for multi-line tool output (50-line unittest dump at lines 41–91, repeated at 93–152) the log copy completely defeats the callback's `compact_limit` truncation.

Root cause: `agent.py:_setup_logger` attaches `console_handler = StreamHandler(sys.stdout)` at `INFO` level alongside a file handler at `DEBUG`. Five `log.info` calls in the main loop duplicate events the callback already renders.

## Success metric

- **Baseline**: 206 lines in `/tmp/agent-cicd/probes/0008-bug-before.log` (P-bug probe, current HEAD).
- **Target**: re-running the same P-bug probe against the worktree's `agent.py` produces a log of **≤ 155 lines** (−25% or better). Informal stretch goal: ~130 lines, which is what dropping the five duplicated events predicts.
- **Measurement method**:
  ```bash
  wc -l /tmp/agent-cicd/probes/0008-bug-after.log
  ```
  Plus sanity checks that the session log file under `.agent/history/session_*.log` still contains the full untruncated `TOOL RESULT` / `ASSISTANT` / `TOOL CALL` / `USER` entries (via the DEBUG file handler), so post-mortem debugging isn't lost.

## Scope

- **In**:
  - `agent.py` — demote the five duplicated `log.info(...)` calls to `log.debug(...)`:
    - `log.info("USER: %s", ...)` at lines 1309 and 1400
    - `log.info("ASSISTANT: %s", ...)` at line 1638
    - `log.info("Executing %d tool call(s)", ...)` at line 1730
    - `log.info("TOOL CALL: %s(%s) [id=%s]", ...)` at line 1783
    - `log.info("TOOL RESULT [%s]: %s", ...)` at line 1850
  - `tests/test_agent_console_dedup.py` — new regression test asserting those five messages are not emitted at `INFO` level but remain at `DEBUG` level so the file handler still captures them.

- **Out**:
  - Any change to `callbacks.py` rendering, truncation, theme, or `compact_limit`.
  - Any change to the logging setup itself (handlers, levels, file rotation, formatters).
  - Any change to other `log.info` calls (session start/end, turn headers, response status, summary, auto-nudge, hallucination guards, forced-think, cancellation, continue-mode banners, etc.) — those are status events, not renderer duplicates, and should stay at INFO so the console keeps showing them.
  - Reformatting or renaming.

## Implementation steps

1. Open `agent.py` and apply the five `log.info` → `log.debug` demotions exactly at the call sites named above. Each is a one-word change; no surrounding code moves.
2. Run `python3 -m unittest discover tests 2>&1 | tail -10` and confirm the current 114 tests still pass.
3. Write `tests/test_agent_console_dedup.py`:
   - Import `agent` as a module (the repo already imports agent.py directly in some tests — follow the same style).
   - Use `unittest` + `self.assertLogs("agent", level="DEBUG")` to sanity-check that when a fake log record with the banned templates is emitted at `log.info`, no DEBUG-or-above record fires at INFO — or, more simply, grep the source text of `agent.py` for the five call sites and assert each is `log.debug(...)` not `log.info(...)`. A source-text assertion is stable against future refactors: it catches a regression to `log.info` exactly where the bug lives.
   - Preferred shape: a single test that reads `agent.py` as text and asserts each of the five templates ("USER: %s", "ASSISTANT: %s", "Executing %d tool call(s)", "TOOL CALL: %s(", "TOOL RESULT [%s]:") appears only as `log.debug(...)`, never as `log.info(...)`. This is the same approach cycle 0005/0006 used for the SHARED RUNTIME regression test.
4. Re-run the full suite and confirm 115 passing (114 + 1 new).
5. Re-run the P-bug probe in a fresh temp dir against the worktree's `agent.py` and capture to `/tmp/agent-cicd/probes/0008-bug-after.log`.
6. Compute `wc -l` on both logs and record the delta. Sanity-check the session log file under `.agent/history/` still contains the full `TOOL RESULT` / `ASSISTANT` records at DEBUG.

## Test plan

- **Existing tests that must stay green**: all 114 under `tests/`. Particular attention to anything that imports `agent.py` or exercises the main loop.
- **New tests**: `tests/test_agent_console_dedup.py` — one test that parses `agent.py` source and asserts the five duplicated templates live on `log.debug(...)` lines, never `log.info(...)`. Rationale for text-level assertion: the call sites are the ground truth; a behavioral test that spins up the full main loop just to catch this is much more fragile than a grep.
- **Probe re-run**: P-bug against `/tmp/probe-0008-bug/` (re-seed stats.py with the `+2` bug first). Expected delta: log line count 206 → ≤ 155, tool-call count unchanged at 5, verdict PASS.
- **Sanity check on the session log file** (post-run): `grep -c 'TOOL RESULT' /tmp/probe-0008-bug/.agent/history/session_*.log` should be ≥ 5 even though the console shows zero — proves the DEBUG file handler still captures everything.

## Risks & mitigations

- **Risk**: demoting `USER: …` to DEBUG means CICD-harness logs that grep for `INFO: USER:` would miss it. **Mitigation**: grepped the tests tree — no test relies on the INFO-level `USER:`, `ASSISTANT:`, `TOOL CALL:`, `TOOL RESULT:`, or `Executing N tool call(s)` strings. External tooling (if any) can grep the session log file instead, which still has them at DEBUG.
- **Risk**: a future reader sees the `log.debug` calls and assumes they're dead code. **Mitigation**: the session log file is at DEBUG level, so these lines do still appear there for post-mortem. A short comment on at least the `TOOL RESULT` call site explaining "debug-level so it doesn't duplicate the callback render" keeps the intent legible.
- **Risk**: the probe's line count has variance (e.g., turn count differs by one between runs). **Mitigation**: the gap between 206 and 155 is 51 lines — larger than any plausible run-to-run variance for a 5-tool-call probe. If the after-run somehow lands above 155, re-run once; if still over, treat as a null-result and analyze.

## Rollback

`git checkout -- agent.py tests/test_agent_console_dedup.py` on the worktree branch undoes everything. The fix is five one-word edits plus one new test file; no schema, no API surface, no external integrations touched.

## Closes

Closes #15
