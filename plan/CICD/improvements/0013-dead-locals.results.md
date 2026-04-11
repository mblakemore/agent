# 0013 — dead-locals — results

- Issue: #24
- Branch: cicd/0013-dead-locals
- PR: #(pending) (will be filled in after `gh pr create` in TRACK)
- Commit range: 9ceaf39..<final>
- Date: 2026-04-11

## Metric

- **Baseline**: static dead-local scan across the 3 target files → **5** hits
  (`tools/file.py::_resolve_path` × 2, `agent.py::run_agent_single` × 1,
  `agent.py::_AsyncSummarizer.kick._worker` × 1, `tool_recovery.py::_ask_for_param` × 1)
- **After**: **0** hits
- **Delta**: **−5 (−100%)**
- **Measurement**: `tests/test_no_dead_locals.py::TestNoDeadLocals::test_guarded_functions_have_no_dead_locals`
  walks each function with `ast`, collects `Name(Store)` minus `Name(Load)`
  after filtering function args, for-loop targets, AugAssign targets,
  walrus targets, and exception-handler bindings, and asserts the
  remaining set is empty per function.

## Test suite

- Before: **120** passing
- After:  **121** passing (+1 — the new regression guard)

## Probe re-run

The cycle's primary metric is the dead-local scan itself, not a probe
metric — cycle 0011 proved the P-bug tool-call count has ±3-call
run-to-run variance on this model, which would swamp any behavior
delta from a code-quality cleanup that doesn't touch hot paths. The
probe re-run below is informational only, to confirm no regression.

- **Before log**: `/tmp/agent-cicd/probes/0013-pbug-before.log`
  (5 tool calls, 6 turns, verdict PASS — ran against main HEAD 5d47d61)
- **After log**: `/tmp/agent-cicd/probes/0013-pbug-after.log`
  (5 tool calls, 6 turns, verdict PASS — ran against worktree agent.py)
- **Delta**: 0 tool calls, 0 turns. Expected — deleting dead locals
  does not touch runtime behavior.
- **Secondary verification**: manually drove `_resolve_path` through
  the three branches (normal relative, absolute, cwd-prefix-dedup) in
  the worktree's `tools/file.py`. All three still return the expected
  `Path` value.

## What I actually changed

- Deleted 5 dead local assignments across 3 files (5 lines net removed).
- Added `tests/test_no_dead_locals.py` — a 125-line AST-based regression
  guard scoped to 4 whitelisted functions with common false-positive
  filtering (args, for-targets, AugAssign, walrus, except-handlers).
- The stale comment `# Check if the relative path starts with parts of the cwd`
  in `_resolve_path` was kept because it still describes the remaining
  `if path.startswith(str(cwd)[1:])` check on the next line — only the
  two dead assignments above/between it were removed.

## What I learned

- The `_resolve_path` cleanup incidentally removes a `.resolve()` syscall
  from the file-tool hot path. Not a targeted optimization, but a real
  micro-win that was hiding behind a dead assignment.
- The guarded-function approach (AST walk on a small whitelist) is a
  good fit for CICD regression tests: narrow enough to stay deterministic,
  broad enough to catch real re-introduction. Future cycles that clean
  up code smells in specific functions should follow the same pattern
  rather than trying to grep-guard repo-wide from the start.
- The nested-function case (`_worker` inside `kick`) works fine with
  `ast.walk` because `FunctionDef` is discoverable by name anywhere in
  the tree. The scoping concern (that `_worker` would see its outer
  scope's names) is handled by the `child is not func` short-circuit
  that skips nested `FunctionDef` children during the walk.
