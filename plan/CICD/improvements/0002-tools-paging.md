# 0002 — tools-paging

**Issue**: #1 — friction: /tools command only ever shows 20 of 50 buffered calls, no way to see more
**Branch**: `cicd/0002-tools-paging` (will be created in Phase 6)

## Goal

Make `/tools` show the entire buffered tool history by default, and accept `/tools N` to request only the most recent N entries. No tool call the buffer is already holding should be invisible to the user.

## Motivation

Issue #1 has the full triage. Summary:

- `callbacks.TerminalCallbacks.tool_history` is a `deque(maxlen=50)` (`callbacks.py:172-177`).
- `render_tools(limit: int = 20)` slices the deque with `tail = list(self.tool_history)[-limit:]` (`callbacks.py:371-381`), so by default 30 of the 50 buffered entries are unreachable from the CLI.
- `_cmd_tools` in `commands.py:72-74` calls `render_tools()` with no argument, locking in the 20 default.
- `handle_command` in `commands.py:87-103` keys on `line.strip()` (the full post-strip string), so even if the handler accepted a limit there is no syntax to pass one — `"/tools 40"` falls through to the unknown-command branch.

The floor cause is the dispatcher's exact-match lookup. Fixing only `render_tools`'s default would partially solve the problem but not give the user a way to narrow the view when 50 entries are too noisy. Fixing only the default would also silently regress anyone who actually wanted the old 20-entry digest.

I'll fix both: default to **all** when no argument is given (so the buffered data is reachable), and let `/tools N` clamp to the most recent N when the user wants less. `/tools all` is an explicit alias for "show everything" so the help text can demonstrate both forms.

Probe log: `/tmp/agent-cicd/probes/0002-pcount-before.log` — probe cycle (P-count) succeeded, no new bugs found during PROBE. Baseline for this issue is captured by direct code inspection and the unit test added in Phase 6 rather than a live-agent probe (the metric is structural: "how many entries does `/tools` surface").

## Success metric

- **Primary**: number of tool-call entries visible in `/tools` output when the buffer holds 50 calls.
  - **Baseline**: 20 (hard-coded default in `render_tools`)
  - **Target**: 50 (the full buffered history)
  - **Measurement command**: new unit test `tests/test_tools_paging.py::test_tools_no_arg_shows_entire_buffer` — populates 50 tool results via `on_tool_result`, captures stdout of `handle_command("/tools", ctx)`, counts entries of the form `r"  [✓✗] (\d+)\. "` in the captured output. Asserts max index == 50.
- **Secondary** (reported, not gating):
  - Unit test count: 86 → 89 passing (3 new tests for no-arg, `/tools 5`, `/tools bogus`).
  - Manual-UX metric from the issue: "steps required to see the 30th most-recent tool call" — before: impossible, after: 1 (type `/tools`).

## Scope

**In**:

- `commands.py`
  - Change `handle_command` to split the stripped input into `(verb, rest)` on the first whitespace; look up `verb` in `_COMMANDS`; pass `rest` (stripped) to the handler as a second positional arg.
  - Change the handler type alias from `Callable[[SimpleNamespace], None]` to `Callable[[SimpleNamespace, str], None]`.
  - Update every handler (`_cmd_help`, `_cmd_clear`, `_cmd_context`, `_cmd_model`, `_cmd_verbose`, `_cmd_tools`) to accept `(ctx, args)`. All but `_cmd_tools` simply ignore `args`. (If any handler receives non-empty args it does not understand, it warns via `on_notice` so typos like `/clear now` don't silently drop characters.)
  - `_cmd_tools` parses `args`:
    - empty → `render_tools(limit=None)` (show all)
    - `"all"` (case-insensitive) → `render_tools(limit=None)`
    - positive integer → `render_tools(limit=int(args))`
    - anything else → `on_notice("warn", "usage: /tools [N|all]")` and return
  - Update `_cmd_help` to list the `/tools [N|all]` form.

- `callbacks.py`
  - Change `render_tools(self, limit: int = 20)` to `render_tools(self, limit: Optional[int] = None)`. When `limit` is `None`, show every entry in `tool_history`.
  - Update the header text: when showing all, print `"All N tool call(s):"`; when clamped, print `"Last min(limit, N) of N tool call(s):"`. This makes it obvious from a glance whether the view is complete.
  - Import `Optional` from `typing` if not already imported (it is already imported at the top of callbacks.py — confirm during implementation).

- `tui.py`
  - Update the `SLASH_COMMANDS` tuple (line 77-79 in the current layout) so the `/tools` description reflects the optional arg: `("/tools",   "show recent tool calls — /tools N or /tools all")`. This is only a display string for the completer; no behavior change.

- `tests/test_tools_paging.py` (new file)
  - `test_tools_no_arg_shows_entire_buffer` — populates 50 results, calls dispatcher, asserts all 50 entries render.
  - `test_tools_with_integer_limits_view` — populates 50, calls `/tools 5`, asserts exactly 5 entries render and they are the 5 most recent.
  - `test_tools_all_alias_shows_everything` — populates 50, calls `/tools all`, asserts all 50 entries render.
  - `test_tools_bogus_arg_warns_without_crashing` — calls `/tools xyzzy`, asserts dispatcher returns True, no entries printed, warning emitted.
  - `test_render_tools_limit_none_shows_full_history` — unit-level against `callbacks.TerminalCallbacks`, not the dispatcher — documents the new `limit=None` contract.

- `tests/test_commands.py`
  - Update existing `test_tools_calls_render_tools` — still passes under the new signature because `/tools` with no args still prints the single tool entry. No functional change needed; double-check during implementation and only touch it if it breaks.
  - Add `test_unknown_arg_for_nonarg_command_warns` — calls `/clear stray args`, asserts history still clears but a warn notice is emitted. This pins the "extra args are not silently swallowed" behavior so a future change doesn't regress it.

- `tests/test_callbacks.py`
  - Existing `test_render_tools_empty` still passes (`render_tools()` returns the "No tool calls yet." string regardless of limit).
  - Existing `test_render_tools_populated` — currently asserts `file` and `line1` appear in output with no limit arg. Still holds under `limit=None` default. Do not modify unless it breaks.

**Out**:

- Persisting tool history to disk (out of scope — buffer-only).
- Changing `tool_history_size` from 50 (that's a separate sizing decision).
- Refactoring every other slash command to take meaningful args — this cycle only touches what `_cmd_tools` needs and lightly updates the others to the `(ctx, args)` signature.
- Issue #2 (extra-tools dead call) — already closed by cycle 0001.

## Implementation steps

1. Create worktree `cicd/0002-tools-paging` under `/tmp/agent-cicd/0002-tools-paging`.
2. Edit `callbacks.py`:
   - Change `render_tools` signature default to `limit: Optional[int] = None`.
   - When `limit is None`, set `tail = list(self.tool_history)` and header to `"All N tool call(s):"`.
   - When `limit is not None`, keep the existing slicing path with a clarified header.
3. Edit `commands.py`:
   - Update `_COMMANDS` value type alias to `Callable[[SimpleNamespace, str], None]`.
   - Add a parameter `args: str = ""` to every handler; existing handlers keep their bodies but warn if `args` is non-empty via `safe_cb(ctx.cb, "on_notice", "warn", f"{verb} takes no arguments — got: {args!r}")`.
   - Rewrite `_cmd_tools` to parse `args` (empty / "all" / integer / bogus).
   - Rewrite `handle_command` to split verb + rest and pass both to the handler.
   - Extend `_cmd_help` text to advertise `/tools [N|all]`.
4. Edit `tui.py` `SLASH_COMMANDS` entry for `/tools` to reflect the new usage hint.
5. Run the full suite once to see what breaks. Fix any mismatch.
6. Write `tests/test_tools_paging.py` with the five tests listed above.
7. Add `test_unknown_arg_for_nonarg_command_warns` to `tests/test_commands.py`.
8. Run the full suite — must be 89/89 green. Re-run the P-count probe from Phase 2 to confirm no regression in end-to-end agent behavior (turn count should still be ≤3, wall time within 2× of the 4.67s baseline).
9. Commit in small steps on the branch.

## Test plan

- **Existing tests that must stay green** (all 86): `tests/test_callbacks.py`, `tests/test_cancel.py`, `tests/test_commands.py`, `tests/test_file_refs.py`, `tests/test_load_extra_tools.py`, `tests/test_spinner.py`, `tests/test_tui.py`.
- **New tests** — see the list above in Scope → `tests/test_tools_paging.py` (5 tests) plus one new test in `tests/test_commands.py`. Net new passing tests: **+6** (target 86 → 92, well above the minimum of 89).
- **Re-run probe**: P-count from Phase 2. Expected delta: wall time within ±1s, turn count unchanged (2), tool calls unchanged (1), verdict PASS. The probe is not directly measuring the metric; it is a regression guard that proves the signature changes to the dispatcher did not break the end-to-end interactive loop.

## Risks & mitigations

- **Risk**: Some caller of `render_tools` elsewhere in the repo passes a positional int and now the default semantics change. **Mitigation**: Only `commands.py:74` and the tests call `render_tools`; verified via `grep -rn 'render_tools' --include='*.py'`. No third-party caller to worry about.
- **Risk**: Changing the handler signature breaks TUI keystroke bindings. **Mitigation**: `tui.py` does not call handlers directly — it calls `handle_command(line, ctx)` just like the terminal loop (`agent.py:1388`). The dispatcher is the single entry point, and its external signature is unchanged.
- **Risk**: `_cmd_help`'s text is asserted in `test_help_prints` (it asserts `/help`, `/clear`, `/verbose`). Adding the `[N|all]` hint to `/tools` only extends the text, it doesn't remove those strings, so the assertion stays true.
- **Risk**: Emitting a warn when a no-arg command receives trailing text could annoy users who pasted a newline-stripped line. **Mitigation**: The warn only triggers when `args` is a non-empty stripped string, so whitespace-only trailing is still fine. And the command still executes — we warn, we don't block.
- **Risk**: `render_tools(limit=None)` rendering 50 colored entries might overflow a small terminal. **Mitigation**: That's exactly what the user asked for by typing `/tools`; the whole point of the fix is to surface the full buffer. Users who want less can type `/tools 10`. Not a real risk, documented in help text.

## Rollback

One-line revert on the branch: `git revert <commits>`. The underlying behavior change is small and localized to two source files. The new test file can simply be deleted. No data migrations, no config schema change. If the full loop finds a regression that can't be fixed in ≤3 debug iterations, follow the null-result path in Phase 8.

## Closes

Closes #1
