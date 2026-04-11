## 0019 — dead-null-hooks — results

- Issue: #42
- Branch: cicd/0019-dead-null-hooks
- PR: (pending)
- Commit range: df38c13..HEAD
- Date: 2026-04-11

## Metric

- **Baseline**: **5** dead hook stubs on `NullCallbacks` (`on_session_end`, `on_api_start`, `on_api_response`, `on_api_done`, `on_turn_end` — each declared in `callbacks.py` with zero non-definition callers anywhere in the repo). Baseline log: `/tmp/agent-cicd/probes/0019-dead-null-hooks-before.log`.
- **After**: **0**.
- **Delta**: **−5 (−100%)**.
- **Measurement script** — the same command from the plan, re-run against worktree HEAD:
  ```bash
  python3 -c "
  import ast, re, os
  tree = ast.parse(open('callbacks.py').read())
  hooks = []
  for n in ast.walk(tree):
      if isinstance(n, ast.ClassDef) and n.name == 'NullCallbacks':
          for item in n.body:
              if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                  name = item.name
                  if name.startswith('_') or name == 'check_cancelled': continue
                  hooks.append((item.lineno, name))
  dead = 0
  for lineno, h in hooks:
      hits = 0
      for root, _, files in os.walk('.'):
          if '/.git' in root: continue
          for fn in files:
              if not fn.endswith('.py'): continue
              p = os.path.join(root, fn)
              for i, line in enumerate(open(p), 1):
                  if re.search(r'\b' + re.escape(h) + r'\b', line):
                      if not (p == './callbacks.py' and i == lineno): hits += 1
      if hits == 0: dead += 1
  print(dead)
  "
  ```
  Output: `0`. After log: `/tmp/agent-cicd/probes/0019-dead-null-hooks-after.log`.

## Test suite

- Before: 128 passing.
- After:  129 passing (+1 regression guard `TestHookInterfaceShape::test_no_dead_null_callbacks_hooks`).
- Full log: `/tmp/agent-cicd/probes/0019-tests-after.log`.

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0019-dead-null-hooks-after.log`
- Verdict: **PASS**

## Sabotage verification

Before committing the regression test I sabotaged `callbacks.py` by inserting a fresh `on_totally_dead_hook` stub into `NullCallbacks` only (count=1 on `str.replace`) and ran `python3 -m unittest tests.test_callbacks.TestHookInterfaceShape.test_no_dead_null_callbacks_hooks`. The test failed with:

```
AssertionError: Lists differ: ['on_totally_dead_hook'] != []
... NullCallbacks declares hook stubs that no call site in the repo ever
invokes (via _emit, safe_cb, or direct attribute access):
['on_totally_dead_hook']. Either wire them up or delete the stubs.
```

The guard catches the exact defect class this cycle cleaned up.

## What I actually changed

- `callbacks.py` — deleted 5 method stubs from `NullCallbacks`: `on_session_end`, `on_api_start`, `on_api_response`, `on_api_done`, `on_turn_end` (16 lines). No other method bodies touched. Section-comment headers (`--- session lifecycle ---`, `--- LLM API lifecycle ---`, `--- per turn ---`) are cleaned up where they became empty (the `--- per turn ---` header is removed entirely since `on_turn_end` was its only member; the other two still host live hooks and the headers stay). `Any` import in `from typing import Any, Optional` stays — `safe_cb` at the bottom of the file still uses it.
- `tests/test_callbacks.py` — added `TestHookInterfaceShape::test_no_dead_null_callbacks_hooks` (58 lines). Walks `NullCallbacks` via AST, greps every `.py` file under the repo root (excluding `.git`) for each non-underscore, non-`check_cancelled` hook, and fails with the list of stubs that have zero callers.

## What I learned

- The `NullCallbacks` interface carried 5 ghost hooks from the original UI-upgrade design sketch in `plan/ui-upgrade-from-llmbox-cli.md:107-122`. The upgrade landed without wiring them and 15+ CICD cycles touching `callbacks.py` since then never noticed. A "every declared hook has at least one caller" invariant is cheap to enforce and would have prevented the stubs from outliving the design.
- Sabotage-verifying regression guards matters. On the first pass I used `str.replace()` without `count=1` and replaced `def on_session_start` in both `NullCallbacks` and `TerminalCallbacks`, which made the fake hook appear in two places and fooled me into thinking the guard was broken. The guard is correct — my sabotage was wrong. Lesson: sabotage with a precise edit, not a broad search-and-replace.
- This is the same defect family as cycles 0014 (dead imports), 0016 (dead `log` on `_summary_request`), 0017 (discarded-arg hooks), and 0018 (dead `reasoning`/`auto` params), just one layer up: dead *methods* instead of dead parameters or dead imports. The dead-interface well is not quite dry — a future cycle could try the same AST-walk approach on `TerminalCallbacks` concrete methods to hunt for dead helpers, or on any non-test `.py` file for dead top-level functions.
