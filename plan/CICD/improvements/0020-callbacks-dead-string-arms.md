# 0020 — callbacks-dead-string-arms

**Issue**: #44 — bug: TerminalCallbacks has 3 dead string-switch arms whose literal is never emitted anywhere in the repo
**Branch**: cicd/0020-callbacks-dead-string-arms

## Goal

Delete 3 unreachable dispatch arms from `TerminalCallbacks` in `callbacks.py`
and add an AST-based regression guard that fails CI if any new dead arm is
introduced.

## Motivation

Static AST walk (baseline log `/tmp/agent-cicd/probes/0020-dead-branches-before.log`)
plus a dynamic-key scan (`/tmp/agent-cicd/probes/0020-verify-literals.py`)
proves three dispatch arms in `TerminalCallbacks` handle string values that
no `_emit(...)` or `safe_cb(...)` call site in the entire repo produces. Every
call site for these three hooks passes a **constant string literal** as the
switch key (0 dynamic keys), so the unreachable arms are provably dead —
no runtime value can ever hit them.

| Hook | Dead arm | Source line | Emitted literal set |
|---|---|---|---|
| `on_notice(level, msg)` | `elif level == "error":` | `callbacks.py:341-342` | `{"info", "warn"}` |
| `on_hallucination_stripped(kind)` | `else: [hallucination stripped: {kind}]` | `callbacks.py:306-307` | `{"file_read", "text_only"}` |
| `on_overtime(reason)` | `.get(reason, f'[overtime: {reason}]')` fallback | `callbacks.py:313-317` | `{"text_only", "repeated_result"}` |

Same defect class as cycles 0016 (dead `log` param), 0017 (discarded args),
0018 (dead `reasoning`/`auto` params), and 0019 (dead `NullCallbacks` hook
stubs) — but one layer deeper: dead **dispatch arms** inside live methods
instead of dead parameters or dead methods.

## Success metric

- **Baseline**: 3 dead dispatch arms in `TerminalCallbacks`.
- **Target**: 0.
- **Measurement method** — re-runnable grep script:
  ```bash
  cd /mnt/droid/repos/agent
  a=$(grep -cE 'elif level == "error":' callbacks.py)
  b=$(grep -cF '[hallucination stripped: {kind}]' callbacks.py)
  c=$(grep -cF '[overtime: {reason}]' callbacks.py)
  echo $((a + b + c))
  ```
  Current output: `3`. Target output after change: `0`.
- Full script: `/tmp/agent-cicd/probes/0020-baseline-scan.sh` (already captured
  for the baseline run, will be re-run in Phase 7 against the worktree).

## Scope

- **In**:
  - `callbacks.py` — edit 3 method bodies in `TerminalCallbacks`
    (`on_notice`, `on_hallucination_stripped`, `on_overtime`).
  - `tests/test_callbacks.py` — add an AST-driven regression guard
    `TestTerminalCallbacksDispatchArms::test_no_dead_string_switch_arms`
    that walks `_emit`/`safe_cb` call sites for the 3 target hooks,
    collects emitted literals, then asserts the `TerminalCallbacks` method
    body for each hook contains handler arms only for those literals.
- **Out**:
  - `NullCallbacks` (cycle 0019 cleaned it; its stubs return None).
  - `on_summarizer_status` — uses `.get(status, ...)` but every status key in
    the default set is actually emitted, so no dead arm.
  - Other hooks with scalar or non-string-switch bodies.
  - Live call site changes in `agent.py`/`commands.py` (call sites are fine
    as-is; only the callback receiver needs to change).

## Implementation steps

1. **`callbacks.py` — `TerminalCallbacks.on_notice`** (lines 338-344):
   Delete the `elif level == "error":` branch and its body. Keep the `"warn"`
   if and the final `else: self._note(msg)` (which remains alive via the
   emitted `"info"` level).

2. **`callbacks.py` — `TerminalCallbacks.on_hallucination_stripped`**
   (lines 301-307): Delete the `else:` branch and its body. Unknown kinds
   silently no-op, matching `NullCallbacks.on_hallucination_stripped`'s
   pass-through.

3. **`callbacks.py` — `TerminalCallbacks.on_overtime`** (lines 312-317):
   Replace the dict-`.get`-with-fallback pattern with an explicit if/elif
   chain over the 2 known reasons. Unknown reasons silently no-op.

4. **`tests/test_callbacks.py`** — add a new test class
   `TestTerminalCallbacksDispatchArms` with **two** tests:

   **(a) `test_no_known_dead_dispatch_arms`** — cheap, deterministic source
   assertion that the three specific dead-arm markers from the baseline are
   not present in `callbacks.py`:
   - `'elif level == "error":'`
   - `'[hallucination stripped: {kind}]'`
   - `'[overtime: {reason}]'`

   **(b) `test_dispatch_arms_are_reachable`** — AST-driven guard that
   catches *new* dead arms introduced by future edits. For each of the 3
   target hooks:
   - Walks every `.py` file under the repo root (skipping `.git`, `tests/`,
     `plan/`), collecting `_emit("hook", literal, ...)` and
     `safe_cb(cb, "hook", literal, ...)` call sites. Records the set of
     emitted string literals at the switch-key position (positional arg 0).
     Asserts every such call site uses a `Constant(str)` (no dynamic keys).
   - Parses `callbacks.py`, locates `TerminalCallbacks.<hook>`, and walks
     its body to classify each arm:
       - `literal_keys` = set of string literals compared against the
         switch arg in `if/elif arg == "lit":` chains, plus dict literal
         keys used in `dict.get(arg, ...)` / `dict[arg]` expressions.
       - `has_else_fallback` = bool — does the final `if/elif` chain have
         a trailing `else:` branch with a non-pass body?
       - `has_default_fallback` = bool — does the body contain a
         `dict.get(arg, default)` where `default` is a non-None value?
   - Asserts the dead-arm invariant:
       1. `literal_keys ⊆ emitted_set` — every explicitly handled key is
          actually emitted (catches `elif level == "error":` with no
          `"error"` caller).
       2. `has_else_fallback → (emitted_set - literal_keys) != ∅` — the
          else is reachable only if some emitted value isn't covered by
          the explicit chain (catches `on_hallucination_stripped`'s dead
          else).
       3. `has_default_fallback → (emitted_set - dict_keys) != ∅` — the
          default is reachable only if some emitted value isn't a dict
          key (catches `on_overtime`'s dead `.get` default).
   - On failure, reports the hook, the emitted set, the handled set, and
     which invariant was violated with the source line.

5. Re-run the baseline grep script in the worktree, confirm output `0`.

6. Run the full unit test suite inside the worktree and confirm
   `129 + 1 = 130 passing`. Debug if not green.

## Test plan

- **Existing tests that must stay green**: full suite (129 passing) —
  especially `tests/test_callbacks.py::TestNullCallbacks` (not affected;
  this plan only touches `TerminalCallbacks`), the cycle 0017 regression
  in `tests/test_callbacks.py` (threads `"streaming"`/`"exec_command"` args
  through `on_cancelled`/`on_forced_think`/`on_text_loop_detected`;
  unrelated), and cycle 0018's `TestDeadParams` (checks for dead
  `reasoning`/`auto` params; unrelated).
- **Behavioral-level sanity**: cycle-0017 regression test already exercises
  `TerminalCallbacks.on_hallucination_stripped("text_only")` and
  `on_hallucination_stripped("file_read")` via the tests in
  `tests/test_callbacks.py` if present — confirm they still pass with the
  else-branch removed.
- **New test**: `TestTerminalCallbacksDispatchArms::test_no_dead_string_switch_arms`
  in `tests/test_callbacks.py`. On failure lists the hook, the emitted set,
  the handled set, and the dead literals.
- **Re-run probe**: the grep command from "Success metric" — baseline `3`,
  target `0`. After log:
  `/tmp/agent-cicd/probes/0020-dead-branches-after.log`.

## Risks & mitigations

- **Risk**: a third-party subclass overrides one of these hooks via super
  delegation (`super().on_notice("error", ...)`) expecting the base class
  to render rose-colored text.
  **Mitigation**: `TerminalCallbacks` is the canonical concrete UI class
  in this repo; no third-party subclass exists in-tree. `NullCallbacks`
  never rendered anything anyway, so `super()` delegation behavior is
  unchanged for code that subclasses `NullCallbacks`. Re-adding the
  `"error"` branch is one if statement if a future call site needs it,
  and the regression guard will demand it the moment a `_emit("on_notice",
  "error", ...)` call site is added.

- **Risk**: removing the `.get(reason, fallback)` in `on_overtime` changes
  the KeyError semantics if a future caller passes an unknown reason.
  **Mitigation**: the replacement is an if/elif chain (no KeyError path —
  unknown reason is a silent no-op, same as `NullCallbacks`). `safe_cb`
  would have caught a KeyError anyway, so no behavior change.

- **Risk**: the regression test is an AST walk of the whole repo; may be
  slow on CI.
  **Mitigation**: prior cycles' tests (0019 in particular) already walk
  the whole `.py` tree. This test reads the same files. Measured runtime
  on local repo: well under 1 second.

- **Risk**: future refactor adds a hook with a dynamic string key (not a
  literal), making the regression guard's "all emitted keys are literals"
  assumption false and causing a false-negative.
  **Mitigation**: the test explicitly asserts every call site for the 3
  target hooks passes a `Constant(str)`. If a future caller uses a
  dynamic key, the test fails loudly with the file:line of the offending
  call site, prompting the author to either (a) literal-ize the key or
  (b) broaden the regression guard to match the dynamic set.

## Rollback

`git revert <final-commit-on-branch>` restores all 3 dispatch arms and
removes the regression guard in one step. No config, no migration, no
persistent state.

## Closes

Closes #44
