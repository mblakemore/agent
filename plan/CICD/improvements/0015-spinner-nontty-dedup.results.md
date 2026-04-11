# 0015 — spinner-nontty-dedup — results

- Issue: #32
- Branch: cicd/0015-spinner-nontty-dedup
- PR: (pending)
- Commit range: 6a3e448..HEAD
- Date: 2026-04-11

## Metric

- **Primary**: duplication-signature regex on the P-count probe log.
  - Command: `grep -cE '-> [a-z_]+\s+-> [a-z_]+\(' <probe log>`
  - Baseline: **1** (`/tmp/agent-cicd/probes/0015-pcount-before.log`)
  - After:    **0** (`/tmp/agent-cicd/probes/0015-pcount-after.log`)
  - Delta:    **−1 (−100%)**
- **Secondary sanity checks** on the P-count probe:
  - Tool-call count: **1 → 1** (unchanged — one `exec_command` grep)
  - Turn count: **2 → 2** (unchanged)
  - Ground truth match: **122 → 122** (both runs return the correct count)
  - Nothing else in the log diffed other than the header line itself and
    timestamps / log-file paths.

## Test suite

- Before: **122** passing
- After:  **124** passing (122 existing + 2 new in `tests/test_spinner_nontty_dedup.py`)

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0015-pcount-after.log`
- Verdict: **PASS**
- Header line before:
  ```
    -> exec_command   -> exec_command(command='grep -r "^    def test_" /mnt/droid/repos/agent/t…)
  ```
- Header line after:
  ```
    -> exec_command(command='grep -r "^    def test_" /mnt/droid/repos/agent/t…)
  ```

## What I actually changed

- `agent.py` (net +7 lines): replaced the one-line `use_spinner = func_name
  not in _STREAMING_TOOLS` with a gated form that also excludes
  non-interactive mode:

  ```python
  _STREAMING_TOOLS = {"think"}
  use_spinner = (
      func_name not in _STREAMING_TOOLS
      and not theme._no_color()
  )
  ```

  Added a short comment block above the flag that names the specific bug
  (`CLEAR_LINE` empty under NO_COLOR → dangling spinner prefix → callback
  duplicates on the same line). `theme` was already imported at
  `agent.py:42`.

- `tests/test_spinner_nontty_dedup.py` (new, 106 lines): one test class
  `TestSpinnerNonTtyDedup` with two methods:
  1. `test_agent_py_gates_spinner_on_tty` — source-text regex assertion that
     `agent.py` still contains `use_spinner = … and not theme._no_color()`,
     tolerating whitespace and parenthesization. Same pattern cycles 0008
     and 0014 use for their static guards.
  2. `test_on_tool_start_emits_single_header_under_no_color` — behavioral
     assertion: constructs a `TerminalCallbacks`, monkey-patches
     `theme.CLEAR_LINE` to `""` (simulates NO_COLOR), redirects stdout to a
     `StringIO`, calls `on_tool_batch_start → on_tool_start → on_tool_result`,
     asserts `captured.count("-> exec_command(") == 1` and that the
     cycle-0015 duplication signature regex does not match.

Two commits on the branch plus the paperwork commit:

1. `6a3e448` — `agent.py` TTY gate on `use_spinner`
2. `27b075a` — regression test file
3. final — plan + results + progress row

## What I learned

- **Non-interactive code paths deserve the same care as interactive ones.**
  This bug sat in plain sight for every CI / automation run since the
  spinner-per-tool feature landed, because no one who ran interactively
  ever saw the duplication (the `\r\033[K` in `CLEAR_LINE` hid it). Probe
  logs surfaced it instantly — they *are* the non-interactive path the
  bug lives on. A standing rule worth remembering: any
  cursor-control-based UI code should have an explicit test under
  `CLEAR_LINE == ""`, not just under a live TTY.

- **One-scope-down echoes the prior cycle.** Cycle 0008 fixed
  duplicate tool-call rendering at the `log.info`-vs-callback layer (the
  five banned templates). Cycle 0015 fixed it at the spinner-vs-callback
  layer one scope down. Same bug class, same fix shape ("remove the
  redundant emitter; let the canonical callback be the single source of
  truth"). Worth scanning the remaining emitters in `spinner.py`,
  `callbacks.py`, and `agent.py` for a third instance of this pattern
  before it bites.

- **`theme._no_color()` is the right gate for "non-interactive UI".** It's
  a private helper by name (underscore prefix) but it's already the
  authoritative switch that every other cursor-control site in the repo
  depends on (`spinner.py:24`, `theme.py:48`, `tools/think.py` via theme
  helpers). Using it from `agent.py` is consistent with the existing
  private-but-internal convention and avoids introducing a second,
  drift-prone notion of "is this interactive".

- **A two-part guard beats a single static check.** `test_a` catches
  regressions of the agent.py gate. `test_b` catches regressions of the
  callback-side invariant. Either alone would leave one half of the bug
  class undefended. The behavioral half is cheap (no live LLM, no
  subprocess, no reload) because the callback reads `theme.CLEAR_LINE`
  dynamically — a clean monkey-patch is enough.
