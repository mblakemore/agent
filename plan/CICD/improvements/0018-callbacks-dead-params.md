# 0018 — callbacks-dead-params

**Issue**: #40 — bug: dead parameters on TerminalCallbacks.on_assistant_text (reasoning) and on_context_recovery (auto)
**Branch**: `cicd/0018-callbacks-dead-params` (will be created in Phase 6)

## Goal

Drop two dead parameters (`reasoning` on `on_assistant_text`, `auto` on `on_context_recovery`) from `TerminalCallbacks` and `NullCallbacks`, update the two `_emit` call sites in `agent.py`, update the three test lines that still pass the dead `None` second-arg to `on_assistant_text`, and lock the cleanup in place with an AST regression guard.

## Motivation

Static scan of `TerminalCallbacks` (`/tmp/agent-cicd/probes/0018-callbacks-dead-params-before.log`) surfaced 2 parameters whose value is constant-at-call-site and never referenced in the body. Same class as #36 / cycle 0016 (dead `log` threaded through `_summary_request`). Cycle 0017 already surfaced every hook that carried *real* signal; this issue closes the symmetric gap on the ones that don't.

- `on_assistant_text(text, reasoning)` — only call site `agent.py:1626` passes literal `None`. Method body ignores `reasoning`.
- `on_context_recovery(auto)` — only call site `agent.py:1544` passes literal `True`. Method body ignores `auto`.

## Success metric

- **Baseline**: 2 dead parameters on the two hooks (per the AST script in `/tmp/agent-cicd/probes/0018-callbacks-dead-params-before.log`).
- **Target**: 0.
- **Measurement method**:
  ```bash
  cd <WORKTREE>
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
  Expected output after change: `0`.

## Scope

- **In**:
  - `callbacks.py` — `NullCallbacks.on_assistant_text` (line 88), `TerminalCallbacks.on_assistant_text` (line 264), `NullCallbacks.on_context_recovery` (line 140), `TerminalCallbacks.on_context_recovery` (line 335). Drop the dead param from each signature.
  - `agent.py` — line 1626 (`_emit("on_assistant_text", full_content, None)` → `_emit("on_assistant_text", full_content)`), line 1544 (`_emit("on_context_recovery", True)` → `_emit("on_context_recovery")`).
  - `tests/test_callbacks.py` — lines 20, 87, 96 (drop the `None` second arg to `on_assistant_text`). No existing test references `on_context_recovery`, confirmed by grep.
- **Out**:
  - Any other callback hook. Cycle 0017 handled the live-signal hooks; this cycle touches only the two dead ones.
  - Any behavior change in the `_emit` dispatcher itself.
  - `tui.py`, `tool_recovery.py`, `tools/*.py` — grep confirms neither hook is referenced outside the three files above.

## Implementation steps

1. **Worktree**: `git worktree add /tmp/agent-cicd/0018-callbacks-dead-params -b cicd/0018-callbacks-dead-params`.
2. **Edit `callbacks.py`**:
   - `NullCallbacks.on_assistant_text(self, text: str, reasoning: str | None)` → `NullCallbacks.on_assistant_text(self, text: str)`.
   - `TerminalCallbacks.on_assistant_text(self, text: str, reasoning: str | None)` → `TerminalCallbacks.on_assistant_text(self, text: str)`.
   - `NullCallbacks.on_context_recovery(self, auto: bool)` → `NullCallbacks.on_context_recovery(self)`.
   - `TerminalCallbacks.on_context_recovery(self, auto: bool)` → `TerminalCallbacks.on_context_recovery(self)`.
3. **Edit `agent.py`**:
   - Line 1626: `_emit("on_assistant_text", full_content, None)` → `_emit("on_assistant_text", full_content)`.
   - Line 1544: `_emit("on_context_recovery", True)` → `_emit("on_context_recovery")`.
4. **Edit `tests/test_callbacks.py`**:
   - Line 20: `cb.on_assistant_text("txt", None)` → `cb.on_assistant_text("txt")`.
   - Line 87: `cb.on_assistant_text("full text", None)` → `cb.on_assistant_text("full text")`.
   - Line 96: `cb.on_assistant_text("hello", None)` → `cb.on_assistant_text("hello")`.
5. **Add regression guard** in `tests/test_callbacks.py` under `TestTerminalCallbacks`: `test_no_dead_params_on_assistant_text_or_context_recovery`. AST-walks `TerminalCallbacks` and asserts `on_assistant_text` accepts only `self, text` and `on_context_recovery` accepts only `self`. That pins the contract against any future reintroduction.
6. **Run full test suite** inside the worktree: `python3 -m unittest discover tests`. Must be 127→128 passing (+1 regression test, −0 failures).
7. **Re-run measurement script** from the success metric block. Must print `0`.
8. **Commit in two logical chunks**:
   - `CICD 0018 (#40): drop dead 'reasoning'/'auto' params from TerminalCallbacks hooks`
   - `CICD 0018 (#40): regression test — no dead params on on_assistant_text/on_context_recovery`
9. **Final commit** on the branch records plan + results + progress row with body `Closes #40`.

## Test plan

- **Existing tests that must stay green** (all 127):
  - `tests/test_callbacks.py::TestNullCallbacks::test_all_hooks_return_none` — line 20 call site updated.
  - `tests/test_callbacks.py::TestTerminalCallbacks::test_stream_then_assistant_text_no_double_print` — line 87 call site updated.
  - `tests/test_callbacks.py::TestTerminalCallbacks::test_assistant_text_without_stream_prints` — line 96 call site updated.
  - `tests/test_callbacks.py::TestTerminalCallbacks::test_signal_args_surface_in_output` — cycle 0017 regression guard; unaffected by this change (touches different hooks).
  - Every other test — no source dependency on either hook.
- **New test added**:
  - `TestTerminalCallbacks::test_no_dead_params_on_assistant_text_or_context_recovery` — AST-walks `TerminalCallbacks` and asserts the two signatures are `(self, text)` and `(self,)` respectively. Fails loudly if either a dead `reasoning`/`auto`-style param comes back or a live signal is silently threaded.
- **Re-run probe (static)**: the measurement script in the success-metric block is the probe. Expected `2` before, `0` after.

## Risks & mitigations

- **External subclasses** (`TerminalCallbacks` extended by an out-of-tree user) relying on the old signature would break. **Mitigation**: this repo is a single agent binary with no published plugin surface; grep across the tree confirms 3 files reference the two hooks. Documented breaking change is acceptable at this stage, matches the precedent set by #36 / cycle 0016 which also removed a live parameter.
- **`_emit` dispatcher may positional-splat the args** and tolerate extras (unlikely but worth checking). **Mitigation**: before editing, inspect `_emit`'s implementation to confirm it passes its trailing args straight through. If it silently swallows extras, the call-site edits are still safe; if it strict-forwards, they're still safe because the arity is shrinking.
- **Git worktree cleanup** failure if the verify loop aborts. **Mitigation**: rollback block below.

## Rollback

- If verification fails irrecoverably within 3 debug iterations:
  ```bash
  cd /mnt/droid/repos/agent
  git worktree remove /tmp/agent-cicd/0018-callbacks-dead-params --force
  git branch -D cicd/0018-callbacks-dead-params
  ```
  Parent checkout is untouched at HEAD=`dce8871`; no cleanup needed there.
- If the PR lands and a latent downstream consumer breaks, revert is a single commit revert plus a one-liner restoring the dead params — both call sites were pure literals, so a revert restores exact prior behavior.

## Closes

Closes #40
