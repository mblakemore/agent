## 0019 — dead-null-hooks

**Issue**: #42 — bug: NullCallbacks declares 5 hook stubs that no call site in the repo ever invokes
**Branch**: cicd/0019-dead-null-hooks

## Goal

Delete the 5 dead hook stubs from `NullCallbacks` and add a regression guard that prevents new dead hooks from being declared.

## Motivation

AST + full-repo grep (baseline log `/tmp/agent-cicd/probes/0019-dead-null-hooks-before.log`) shows 5 methods declared on `callbacks.NullCallbacks` that have **zero non-definition callers** in every `.py` file in the repo — nothing in `agent.py`, `commands.py`, `tui.py`, or `tests/` ever invokes them via `safe_cb`, `_emit`, or direct attribute access. They are pure interface ghost: readers of `callbacks.py` believe the loop emits them, it does not.

- `on_session_end(info)` — `callbacks.py:46`
- `on_api_start(label)` — `callbacks.py:72`
- `on_api_response()` — `callbacks.py:75`
- `on_api_done()` — `callbacks.py:78`
- `on_turn_end(turn, turn_result)` — `callbacks.py:105`

Same defect class as cycles 0014 (dead imports), 0016 (dead `log` param on `_summary_request`), 0017 (discarded-arg hooks), and 0018 (dead `reasoning`/`auto` params) — but one layer up: dead *method* stubs instead of dead parameters. `check_cancelled` is explicitly out of scope (query-style hook per `callbacks.py:12` docstring).

## Success metric

- **Baseline**: 5 dead hook stubs on `NullCallbacks` (defined but zero non-definition callers anywhere in the repo).
- **Target**: 0.
- **Measurement method** — re-runnable AST + full-repo grep:
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
  Current output: `5`. Target output after change: `0`.

## Scope

- **In**: `callbacks.py` (delete 5 stubs). `tests/test_callbacks.py` (add regression guard that runs the scan in-process and asserts 0).
- **Out**: `check_cancelled` (query hook, separate defect class). `TerminalCallbacks` concrete methods. `plan/ui-upgrade-from-llmbox-cli.md` (design sketch, not API reference — leaving untouched). `_emit` dispatch in `agent.py`.

## Implementation steps

1. Delete `on_session_end`, `on_api_start`, `on_api_response`, `on_api_done`, `on_turn_end` method bodies from `NullCallbacks` in `callbacks.py`. Keep the surrounding section-comment headers only where other live hooks remain in the section. Section headers that become empty (e.g. the `--- LLM API lifecycle ---` block loses 3 of its 4 hooks; the remaining `on_api_retry` stays under it) keep their comment.
2. Add a new test `TestHookInterfaceShape::test_no_dead_null_callbacks_hooks` to `tests/test_callbacks.py` that:
   - Walks `callbacks.NullCallbacks` method names via `inspect.getmembers` (excluding dunders, underscore-prefixed, and `check_cancelled`).
   - For each hook, walks the repo tree relative to the repo root (`tests/` included, `.git/` excluded) looking for any non-definition-line reference to the name in a `.py` file.
   - Asserts every hook has ≥ 1 non-definition caller, otherwise fails with the list of dead hooks.
3. Re-run the baseline command and confirm the output is `0`.
4. Run the full unittest suite and confirm 128 + 1 = 129 passing.

## Test plan

- **Existing tests that must stay green**: `tests/test_callbacks.py` (including `TestNullCallbacks::test_all_hooks_return_none`, which I verified never references the 5 deleted hooks). Full suite = 128 tests; target after = 129.
- **New test**: `TestHookInterfaceShape::test_no_dead_null_callbacks_hooks` in `tests/test_callbacks.py` — AST+grep guard. If someone adds an unwired hook stub in a future PR, this test fails loudly with the stub name.
- **Re-run probe**: the measurement command in "Success metric" — baseline `5`, after `0`. After log: `/tmp/agent-cicd/probes/0019-dead-null-hooks-after.log`.

## Risks & mitigations

- **Risk**: `plan/ui-upgrade-from-llmbox-cli.md:107-122` sketches 4 of these hooks as designed interface surface.
  **Mitigation**: the plan doc is a pre-refactor design sketch, not a contract. The UI upgrade landed without wiring these hooks and none of the 15+ cycles since have needed them. If a future cycle needs one, re-adding a hook stub is 5 lines and the regression guard will happily accept it as soon as a first caller exists. Not touching the plan doc in this cycle — it remains a historical design reference.
- **Risk**: a third-party subclass (outside this repo) may override one of these hooks expecting the loop to call it.
  **Mitigation**: `safe_cb` at `callbacks.py:392-410` already treats missing methods as no-ops (`if fn is None: return None`). Removing the stub in `NullCallbacks` does not crash a subclass — it just means the override is also never called, which was already the state of the world since no caller ever existed. Net behavior is identical.
- **Risk**: the regression test is a full-repo filesystem walk and may be brittle on CI.
  **Mitigation**: it only walks `.py` files relative to the repo root, skips `.git`, reads each file once, and bounds runtime by repo size (under ~50 files). Prior cycles' tests already walk the tree; this one follows the same pattern.

## Rollback

`git revert <final-commit-on-branch>` restores all 5 stubs and removes the regression guard in one step. No config, no migration, no persistent state.

## Closes

Closes #42
