# 0015 — spinner-nontty-dedup

**Issue**: #32 — bug: non-interactive tool spinner header duplicates on_tool_start label on every tool call
**Branch**: cicd/0015-spinner-nontty-dedup

## Goal

In non-interactive mode, stop the tool spinner from emitting a half-line header
that the `on_tool_start` callback then duplicates on the same line. Make
`on_tool_start` the single source of truth for the `  -> name(args)` line, as
cycle 0008 did for the log.info-vs-callback split one scope up.

## Motivation

- Probe `P-count` (cycle 0015) captured every non-streaming tool call emitting
  `  -> <name>   -> <name>(args)` in its log. Full log:
  `/tmp/agent-cicd/probes/0015-pcount-before.log`.
- The duplication comes from two separate emitters on the same line:
  1. `spinner.StreamStatus.start()` non-interactive branch
     (`spinner.py:56-61`) writes the prefix `  -> exec_command ` directly to
     stdout so that "downstream output still has its header". For
     non-streaming tools there *is* no downstream streaming output — the tool
     just runs, `finish()` is called, and the prefix is never overwritten
     because `theme.CLEAR_LINE == ""` under NO_COLOR / no-TTY.
  2. `callbacks.TerminalCallbacks.on_tool_start()` (`callbacks.py:279-280`)
     prints the real `  -> name(args)` line after the tool has already run.
     With `CLEAR_LINE == ""` this is an append, not an overwrite, so both
     prefixes end up on the same physical line.
- Prior art: cycle 0008 (#15) fixed the log.info-vs-callback duplication of
  tool-call / result lines by demoting the duplicate emitter. Same shape here.
- Interactive-TTY users never saw this because `\r\033[K` in `CLEAR_LINE`
  rewinds and rewrites the line each time. Only non-interactive runs are
  affected — but *every* CI probe, automation run, and piped session is
  non-interactive.

## Success metric

- **Primary metric**: `grep -cE '-> [a-z_]+\s+-> [a-z_]+\(' <probe log>` on the
  P-count probe log.
  - Baseline: **1** (from `/tmp/agent-cicd/probes/0015-pcount-before.log`)
  - Target: **0**
  - Delta: **−1 (−100%)**
- **Measurement method**: re-run the same `python3 -u agent.py -a "…"` command
  against the worktree's `agent.py`, pipe stdout to a new log, grep with the
  exact command above. Per cycle 0012's learning, for a grep-metric defect the
  probe re-run is the only verification needed.
- **Secondary guard**: new `tests/test_spinner_nontty_dedup.py` that runs the
  tool-call path under captured non-TTY stdout and asserts the header string
  (`-> fake_tool_name`) appears **exactly once**, not twice. This is a
  behavior-level test, not a static scan — it runs the actual
  `TerminalCallbacks.on_tool_start` + `StreamStatus` interaction.

## Scope

- **In**:
  - `agent.py` — gate `use_spinner` on TTY interactivity so non-streaming
    tools skip `StreamStatus` entirely in non-interactive mode. One line.
  - `tests/test_spinner_nontty_dedup.py` (new) — regression guard.
  - `plan/CICD/improvements/0015-spinner-nontty-dedup.md` (this file).
  - `plan/CICD/improvements/0015-spinner-nontty-dedup.results.md` (after
    verify).
  - `plan/CICD/progress.md` — one row appended.
- **Out**:
  - `spinner.py` — the non-interactive `start()` branch is correct *for the
    streaming path* (it writes `"Assistant: "` before stream tokens arrive
    via `on_stream_chunk`). Leave it alone.
  - `callbacks.py` — `on_tool_start` is the canonical emitter; keep it as-is.
  - `think` tool path — already excluded via `_STREAMING_TOOLS`; think streams
    its own output with its own spinner (`tools/think.py`) and is unaffected.
  - Interactive-TTY rendering — no behavior change; the gate only activates
    the new branch under `theme._no_color()`.

## Implementation steps

1. Edit `agent.py` near line 1785. Current:
   ```python
   _STREAMING_TOOLS = {"think"}
   use_spinner = func_name not in _STREAMING_TOOLS
   ```
   Change to:
   ```python
   _STREAMING_TOOLS = {"think"}
   # In non-interactive mode (NO_COLOR / not a TTY) the spinner's
   # non-interactive start() branch writes a dangling "  -> name " prefix
   # that `on_tool_start` then duplicates because CLEAR_LINE is empty.
   # Skip the spinner entirely when there's no TTY; on_tool_start is the
   # single source of truth for the header in that mode.
   use_spinner = func_name not in _STREAMING_TOOLS and not theme._no_color()
   ```
   `theme` is already imported at `agent.py:42`.
2. Add `tests/test_spinner_nontty_dedup.py`. Two tests in one class,
   `TestSpinnerNonTtyDedup`:

   **test_a — static gate is present in agent.py**
   - Read `agent.py` as text.
   - Locate the `_STREAMING_TOOLS = {"think"}` line.
   - Assert the next non-blank non-comment line is `use_spinner = func_name
     not in _STREAMING_TOOLS and not theme._no_color()` (allow surrounding
     whitespace — use a regex, not exact match).
   - On failure, the assertion message shows the actual line so future-me
     can see what drifted. This is the same static-scan pattern cycles 0008
     and 0014 use for their regression guards.

   **test_b — behavioral: one header per tool call under NO_COLOR**
   - `monkeypatch.setenv("NO_COLOR", "1")` — forces `theme._no_color()` to
     return True and `theme.CLEAR_LINE` to `""`.
   - `importlib.reload(theme); importlib.reload(spinner);
     importlib.reload(callbacks)` — pick up the new NO_COLOR state. (The
     three modules cache `CLEAR_LINE` at import time, so reload is
     necessary.)
   - Construct `cb = callbacks.TerminalCallbacks()`.
   - Redirect `sys.stdout` to `io.StringIO()` for the duration of the
     emission.
   - Call `cb.on_tool_batch_start(1)` then `cb.on_tool_start("exec_command",
     {"command": "ls"})` then `cb.on_tool_result("exec_command",
     {"command": "ls"}, "file1.txt\nfile2.txt\n", False)`.
   - Restore `sys.stdout`, read the captured buffer.
   - Assert `captured.count("-> exec_command(") == 1` — single tool-call
     header, no duplication.
   - Assert `re.search(r"-> exec_command\s+-> exec_command\(", captured) is
     None` — the exact duplication pattern the bug produced is absent.
   - Cleanup: reload `theme`, `spinner`, `callbacks` one more time with
     `NO_COLOR` cleared so the reload state doesn't leak into the next test.

   Note: test_b verifies *only the callback side* of the fix (the canonical
   emitter still prints exactly once in non-tty mode). test_a is what guards
   the agent.py gate that prevents the duplicate *input* from the spinner.
   The two together cover both the static gate and the callback behavior.
   Running the full spinner+agent interaction in-process is possible but
   would require reloading `agent` module and exercising the tool-call loop
   with a fake LLM stream — too much setup for a regression guard on a
   two-line fix. The probe re-run in step 4 is the end-to-end verification.
3. Run `python3 -m unittest discover tests` from the worktree. All 122+1
   tests must pass.
4. Re-run the P-count probe against the worktree's agent.py into
   `/tmp/agent-cicd/probes/0015-pcount-after.log`. Apply the grep metric and
   confirm it drops from 1 → 0.
5. Write `plan/CICD/improvements/0015-spinner-nontty-dedup.results.md` and
   append one row to `plan/CICD/progress.md`.
6. Commit in this order (one logical change per commit):
   - `CICD 0015 (#32): gate tool spinner on TTY to kill non-interactive header dup`
   - `CICD 0015 (#32): regression test — single tool-call header under non-TTY stdout`
   - `CICD 0015 (#32): record cycle 0015 plan, results, and progress log` (with
     `Closes #32` in the body of the final commit).

## Test plan

- **Existing tests that must stay green** (122 → 123):
  - `tests/test_spinner.py` — confirms spinner phases still work.
  - `tests/test_agent_console_dedup.py` — cycle 0008's guard; ensures no new
    `log.info` templates get re-introduced.
  - `tests/test_callbacks.py` — confirms `on_tool_start` still exists and is
    called.
  - Full suite.
- **New test added**:
  - `tests/test_spinner_nontty_dedup.py::TestSpinnerNonTtyDedup::test_tool_header_prints_once_under_no_color`
  - `tests/test_spinner_nontty_dedup.py::TestSpinnerNonTtyDedup::test_tool_header_prints_once_under_fake_tty`
- **Probe re-run**:
  - `P-count` — same prompt, same dir, grep metric on new log. Expect
    duplication count to drop from **1** → **0** with no other behavior change.
  - Also sanity-check that the tool-call count on P-count stays at **1** (the
    probe should not need more turns because of the spinner removal).

## Risks & mitigations

- **Risk**: removing the spinner in non-interactive mode means the "I'm
  working on X" feedback during long tool calls disappears from non-tty logs.
  **Mitigation**: that feedback already had zero value in non-tty mode — the
  spinner animation can't animate into a pipe, and the elapsed-time counter
  never printed (it's gated on `_interactive`). The only visible output was
  the static prefix, which is exactly the duplicate we're removing. Net: no
  lost information.
- **Risk**: a test or external tool might grep for the duplicated pattern to
  detect tool calls. **Mitigation**: grepped the repo for
  `-> <tool>\s+-> <tool>` and similar — no matches. The canonical "tool-call
  start" string in both tests and session logs is `TOOL CALL: …` (from
  callbacks' `_note` path, demoted to DEBUG by cycle 0008).
- **Risk**: `theme._no_color()` is a private helper (`_`-prefix). **Mitigation**:
  it's already used inside `theme.py` and imported by `spinner.py:24`; reusing
  it in `agent.py` is consistent with the existing private-but-internal
  convention in this repo. Cycle 0008 took the same approach when it reused
  private log templates.
- **Risk**: cycle 0011's nudge-migration lesson — fixing one surface pushes
  friction into a neighbouring one. **Mitigation**: the fix doesn't change
  *what* gets printed; it only removes a duplicate. There's no behavior for
  the model to migrate around. The only visible change is "one header line
  per tool call instead of 1.x".

## Rollback

Revert is trivial — the change is two lines in `agent.py` plus one new test
file. `git revert <final commit>` cleanly undoes it. No schema, no on-disk
state, no external artifact is touched.

## Closes

Closes #32
