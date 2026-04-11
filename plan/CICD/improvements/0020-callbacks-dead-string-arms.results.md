# 0020 — callbacks-dead-string-arms — results

- Issue: #44
- Branch: cicd/0020-callbacks-dead-string-arms
- PR: (to be opened in Phase 8)
- Date: 2026-04-11

## Metric

- **Baseline**: 3 dead dispatch arms in `TerminalCallbacks`
  (`on_notice` "error" elif, `on_hallucination_stripped` unknown-kind
  else, `on_overtime` unknown-reason `.get` default).
- **After**: 0.
- **Delta**: −3 (−100%).
- **Measurement**:
  ```bash
  cd <repo>
  a=$(grep -cE 'elif level == "error":' callbacks.py)
  b=$(grep -cF '[hallucination stripped: {kind}]' callbacks.py)
  c=$(grep -cF '[overtime: {reason}]' callbacks.py)
  echo $((a + b + c))
  ```
- **Before log**: `/tmp/agent-cicd/probes/0020-dead-branches-before.log` (3)
- **After log**:  `/tmp/agent-cicd/probes/0020-dead-branches-after.log` (0)

## Test suite

- Before: 129 passing
- After:  131 passing (+2 regression tests in
  `TestTerminalCallbacksDispatchArms`)

## Probe re-run

- **Static probe** (AST + grep): see before/after logs above.
- **Literal-only verification**:
  `/tmp/agent-cicd/probes/0020-verify-literals.py` confirms every
  `_emit` / `safe_cb` call site for the 3 target hooks passes a
  constant string — zero dynamic keys, so the dead arms are
  provably unreachable.
- **Regression test fail-proof**: the new AST-driven guard was
  validated by temporarily reintroducing the `on_notice` "error"
  elif arm. Both new tests (`test_no_known_dead_dispatch_arms`
  and `test_dispatch_arms_are_reachable`) failed with clear
  diagnostics, including `AssertionError: {'error'} is not false :
  on_notice: literal(s) ['error'] are handled but no call site
  emits them (emitted=['info', 'warn'])`. Fix was re-applied and
  both tests pass.
- Verdict: **PASS**.

## What I actually changed

- `callbacks.py` — 3 hook bodies trimmed in `TerminalCallbacks`:
  - `on_notice`: deleted `elif level == "error":` branch
    (2 lines). `else:` fallthrough remains alive via emitted
    `"info"` level.
  - `on_hallucination_stripped`: deleted `else:` branch (2 lines).
    Unknown kinds now silently no-op, matching `NullCallbacks`.
  - `on_overtime`: replaced `mapping.get(reason, fallback)` with an
    explicit `if/elif` chain over the 2 emitted reasons. Unknown
    reasons silently no-op.

- `tests/test_callbacks.py` — added class
  `TestTerminalCallbacksDispatchArms` with two tests:
  - `test_no_known_dead_dispatch_arms` — source-level assertion
    that the 3 specific dead-arm marker strings do not reappear.
  - `test_dispatch_arms_are_reachable` — AST-driven guard that
    walks every `_emit` / `safe_cb` call site, collects emitted
    literals per hook, walks `TerminalCallbacks.<hook>` body to
    classify arms (`literal_keys`, `has_else_fallback`,
    `has_default_fallback`), and asserts three invariants:
      1. every literal handled must be emitted,
      2. else-fallback is reachable only if some emitted key isn't
         covered by the explicit chain,
      3. dict-get-default is reachable only if some emitted key
         isn't a dict key.

## What I learned

- **Same defect class, one layer deeper**: cycles 0016–0019 cleaned
  up dead params, discarded args, and dead hook stubs at the
  *interface* level. This cycle extends the cleanup to the
  *dispatch-arm* level inside live methods — same logic (no
  runtime exerciser = remove), one layer of granularity finer.
- **Dynamic-key scan is the enabling proof**: the removal is safe
  only because every call site passes a `Constant(str)`. Without
  that 0-dynamic-keys confirmation, the dead arm is only
  "dead in the call graph we can see" — a runtime-computed level
  could hit it. Future cycles of this shape should run the
  literal-only verification as a prerequisite.
- **AST classifier needs inline-dict-literal assumption**: my
  `has_default_fallback` detector only catches `{...}.get(arg, default)`
  patterns where the dict is an inline literal (e.g.,
  `on_summarizer_status`). It misses the `mapping = {...}; mapping.get(arg, default)`
  two-line variant that pre-edit `on_overtime` used. The
  `test_no_known_dead_dispatch_arms` marker test catches specific
  regressions regardless, so the coverage gap is benign for now,
  but future cycles extending this class of guard should extend
  the classifier to track locally-bound dict names if a new dead
  arm of that shape is introduced.
