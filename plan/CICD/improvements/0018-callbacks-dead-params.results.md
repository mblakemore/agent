# 0018 — callbacks-dead-params — results

- Issue: #40
- Branch: cicd/0018-callbacks-dead-params
- PR: (pending)
- Commit range: 7dd0215..HEAD
- Date: 2026-04-11

## Metric

- Baseline: **2** dead parameters — `TerminalCallbacks.on_assistant_text(reasoning)` + `on_context_recovery(auto)`, each always-literal at the sole call site.
- After: **0**.
- Delta: **−2 (−100%)**.
- Measurement script (re-runnable against worktree HEAD):
  ```bash
  python3 -c "
  import ast, re
  src=open('callbacks.py').read()
  tree=ast.parse(src)
  count=0
  for cls in ast.walk(tree):
      if isinstance(cls,ast.ClassDef) and cls.name=='TerminalCallbacks':
          for fn in cls.body:
              if isinstance(fn,(ast.FunctionDef,ast.AsyncFunctionDef)) and fn.name in ('on_assistant_text','on_context_recovery'):
                  args=[a.arg for a in fn.args.args if a.arg not in ('self','cls')]
                  body=ast.unparse(ast.Module(body=fn.body,type_ignores=[]))
                  for a in args:
                      if not a.startswith('_') and not re.search(r'\b'+re.escape(a)+r'\b',body):
                          count+=1
  print(count)
  "
  ```
  Output: `0`. Baseline log: `/tmp/agent-cicd/probes/0018-callbacks-dead-params-before.log`. After log: `/tmp/agent-cicd/probes/0018-metric-after.log`.

## Test suite

- Before: 127 passing.
- After:  128 passing (+1 regression test `TestHookInterfaceShape::test_no_dead_params_on_assistant_text_or_context_recovery`).
- Full log: `/tmp/agent-cicd/probes/0018-tests-after.log`.

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0018-metric-after.log`
- Verdict: **PASS**

## What I actually changed

- `callbacks.py` — dropped `reasoning: str | None` from both `NullCallbacks.on_assistant_text` and `TerminalCallbacks.on_assistant_text`; dropped `auto: bool` from both `NullCallbacks.on_context_recovery` and `TerminalCallbacks.on_context_recovery`. Method bodies untouched.
- `agent.py` — `_emit("on_assistant_text", full_content, None)` → `_emit("on_assistant_text", full_content)` at the end-of-stream dispatch site; `_emit("on_context_recovery", True)` → `_emit("on_context_recovery")` at the context-overflow reduce path.
- `tests/test_callbacks.py` — updated three existing `on_assistant_text("…", None)` call sites (line 20 in `TestNullCallbacks`, lines 87 and 96 in `TestTerminalCallbacks`) to match the new arity. Added `TestHookInterfaceShape::test_no_dead_params_on_assistant_text_or_context_recovery` — an `inspect.signature`-based guard that pins both hooks to their current shape on the Null and Terminal variants.

## What I learned

- `_emit(method, *args, **kwargs)` in `agent.py` is deliberately variadic, so shrinking a hook arity is safe at every call site without touching the dispatcher — the same property that let cycles 0016, 0017 and now 0018 touch hook signatures without risk.
- The "discarded-live-signal" defect cycle 0017 fixed and the "literal-at-call-site" defect this cycle fixes are two ends of the same dead-parameter spectrum. A single AST scan that walks `TerminalCallbacks` and checks parameter name presence in the body is a cheap, reusable probe — worth re-running any cycle that touches `callbacks.py` in the future.
- Writing the regression guard with `inspect.signature` (rather than string-matching the source) means the guard survives future formatting changes to `callbacks.py` while still failing loudly if a dead param sneaks back in.
