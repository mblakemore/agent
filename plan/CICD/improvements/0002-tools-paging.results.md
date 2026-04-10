# 0002 — tools-paging — results

- Issue: #1
- Branch: cicd/0002-tools-paging
- PR: (opened in Phase 8)
- Date: 2026-04-10

## Metric

**Primary**: entries visible in `/tools` output when 50 tool calls are buffered.

Command:
```
python3 -c "import callbacks, re; cb=callbacks.TerminalCallbacks(); \
  cb._print = lambda *a, **k: None; \
  [cb.on_tool_result(f't{i}', {'i': i}, f'r{i}', False) for i in range(50)]; \
  out = re.sub(r'\x1b\[[0-9;]*m', '', cb.render_tools()); \
  print(len(re.findall(r'^  [\u2713\u2717] (\d+)\. ', out, flags=re.MULTILINE)))"
```

- Baseline: **20** (hard-coded default in `render_tools`; measured by direct inspection of `callbacks.py:371` at HEAD c9416a9)
- After:    **50** (full buffer)
- Delta:    **+30 (+150%)**

Also verified by the new test `tests/test_tools_paging.py::test_tools_no_arg_shows_entire_buffer`, which populates 50 entries, runs `handle_command("/tools", ctx)`, and asserts `_count_entries(plain) == 50` and `"All 50 tool call(s)"` is in the header.

## Test suite

- Before: 86 passing
- After:  94 passing (+8 new)

New tests:
- `tests/test_tools_paging.py::test_tools_no_arg_shows_entire_buffer`
- `tests/test_tools_paging.py::test_tools_with_integer_limits_view`
- `tests/test_tools_paging.py::test_tools_all_alias_shows_everything`
- `tests/test_tools_paging.py::test_tools_bogus_arg_warns_without_crashing`
- `tests/test_tools_paging.py::test_tools_negative_int_rejected`
- `tests/test_tools_paging.py::test_render_tools_limit_none_shows_full_history`
- `tests/test_tools_paging.py::test_render_tools_explicit_limit_clamps`
- `tests/test_commands.py::test_clear_with_stray_args_still_clears_and_warns`

## Probe re-run

Regression probe: **P-count** — "Count every `def test_` function across tests/".

- Before log: `/tmp/agent-cicd/probes/0002-pcount-before.log`
- After log:  `/tmp/agent-cicd/probes/0002-pcount-after.log`
- Ground truth before: 86; agent answered `86`
- Ground truth after (worktree): 94 (86 + 8 new tests); agent answered `94`
- Verdict: **PASS**

| | Before | After |
|---|---|---|
| Wall time | 4.67s | 12.86s |
| Turns | 2 | 7 |
| Tool calls | 1 | ~4 |
| Final answer | 86 ✓ | 94 ✓ |

The after-run took more turns because the model chose a broader exploration path (ls, then grep) instead of going straight to grep. This is model-level variability; my diff does not touch agent-loop code or any tool the auto mode uses. The probe is a regression guard against "did I break the interactive loop?", and the answer is no — the agent still produced the correct count against fresh ground truth.

## What I actually changed

- `callbacks.py:371-388` — `render_tools` now takes `Optional[int]`. When `limit is None`, all buffered entries render and the header says `"All N tool call(s):"`. When `limit` is a positive int, the existing clamp path runs with a clearer `"Last {shown} of {total} tool call(s):"` header. Added `Optional` to the `typing` import.
- `commands.py` — full refactor of the dispatcher:
  - `handle_command` splits the input on the first whitespace run into `verb` and `args`, looks up `verb` in `_COMMANDS`, and passes both to the handler. The external contract (`handle_command(line, ctx) -> bool`) is unchanged.
  - Handler type is now `Callable[[SimpleNamespace, str], None]`. Every existing handler grew an `args: str` parameter.
  - Added `_warn_extra_args(ctx, verb, args)` helper. No-arg commands call it at the top — it is a no-op on empty args and emits a warn notice otherwise, so `/clear now` runs the clear AND surfaces the typo.
  - Rewrote `_cmd_tools` to parse `args`: empty → `limit=None` (full buffer), `"all"` (case-insensitive) → `limit=None`, positive integer → `limit=n`, anything else (non-int, zero, negative) → warn and return without rendering.
  - Extended `_cmd_help` text to advertise `/tools [N|all]`; widened the indent of the verb column to line up the new longer verb.
- `tui.py` — updated the `_SLASH_COMMANDS` entry for `/tools` so the prompt-toolkit completer hint reflects the new form.
- `tests/test_tools_paging.py` — new file, 7 tests covering every branch of `_cmd_tools` plus two unit-level tests of `render_tools`.
- `tests/test_commands.py` — one new test (`test_clear_with_stray_args_still_clears_and_warns`) pinning the "extra args warn but still run" invariant for the non-arg commands.

## What I learned

- The dispatcher's single-line exact-match lookup was the real blocker. Fixing only `render_tools` would have moved the default but left the user no syntax to clamp, which in turn would have forced a follow-up cycle for the `/tools N` UX. Doing both at once is the same diff surface and kills two birds.
- Changing a handler signature ripples through every existing handler, but the change was mechanical and the tests caught all six on the first green run. This is the value of a decent test suite — a signature change felt safe.
- The "warn on extra args and still run" rule is a small UX design decision I made during implementation. The alternative ("refuse and do nothing") would silently eat the user's intent; the chosen behavior runs what they meant and tells them about the typo. Pinned with a test so a future rewrite can't regress it.
- P-count is a good cheap regression probe but it's sensitive to model exploration strategy (2 turns vs 7). For a cycle touching interactive loop code I'd want a probe that exercises slash commands directly; for a cycle touching only dispatch plumbing, P-count's role is "did the import chain still work?" and that's enough.
