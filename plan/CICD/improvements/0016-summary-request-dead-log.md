# 0016 — summary-request-dead-log

**Issue**: #36 — bug: `_summary_request` has dead `log` parameter passed by 5 call sites
**Branch**: cicd/0016-summary-request-dead-log

## Goal

Remove the dead `log` positional parameter from `_summary_request` in `agent.py` and stop every caller from passing a `log` identifier that the function body never reads.

## Motivation

Static scan during PROBE found `_summary_request(prompt, log, base_url=None, model=None)` at `agent.py:553`. The body (lines 561-580) never references `log`; the docstring's `Args:` block doesn't even list it. Six call sites pass `log` (or `self._log`) as the dead positional — every one of them is pure noise that gets dropped on the floor. Same defect family as cycles 0013 (#24 dead-locals) and 0014 (#28 dead-imports); this is a straight cleanup continuation.

Issue body: https://github.com/mblakemore/agent/issues/36

## Success metric

- **Baseline**: dead-parameter score = **7** (1 dead parameter in signature + 6 call sites passing a dead `log` / `self._log` positional)
- **Target**: dead-parameter score = **0**
- **Measurement method**:

```bash
python3 -c "
import ast, re
src = open('agent.py').read()
tree = ast.parse(src)
dead_params = 0
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef) and n.name == '_summary_request':
        args = [a.arg for a in n.args.args]
        body_src = ast.unparse(n.body)
        if 'log' in args and not re.search(r'\blog\b', body_src):
            dead_params += 1
call_dead = 0
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_summary_request':
        pos = n.args
        if len(pos) >= 2 and isinstance(pos[1], ast.Name) and 'log' in pos[1].id:
            call_dead += 1
        elif len(pos) >= 2 and isinstance(pos[1], ast.Attribute) and 'log' in pos[1].attr:
            call_dead += 1
print(dead_params + call_dead)
"
```

Baseline run prints `7`. Target run prints `0`.

## Scope

- **In**:
  - `agent.py:553` — remove `log` from `_summary_request` signature.
  - `agent.py:598`, `:662`, `:664`, `:672`, `:721`, `:725` — drop the `log`/`self._log` positional at every call site.
  - `tests/test_summary_request_signature.py` (new) — AST-based regression guard asserting the score is 0.
- **Out**:
  - `_condense_summary(text, log=None)` (line 583) — its own `log` parameter IS used legitimately (lines 587-588, 601-605, 609-610). Not touched.
  - Any behavioral change to the summary flow, network request, prompt shape, or retry logic.
  - `_summary_request`'s other parameters (`base_url`, `model`) — both are kwargs, both are used.

## Implementation steps

1. Edit `agent.py:553` to drop `log` from the signature: `def _summary_request(prompt, base_url=None, model=None):`.
2. Update each call site to drop the `log`/`self._log` positional:
   - `:598` — `_summary_request(prompt)`
   - `:662` — `_summary_request(prompt)`
   - `:664` — `_summary_request(prompt, base_url=BASE_URL, ...)` (keep kwargs)
   - `:672` — same as :664
   - `:721` — `_summary_request(prompt)`
   - `:725` — same shape (kwargs only)
3. Re-check the docstring — it already omits `log`, so nothing to edit there. Leave the docstring as-is.
4. Add `tests/test_summary_request_signature.py` with one test that runs the exact AST scan from the measurement method and asserts score == 0. Import via file-parsing (no `import agent`) to avoid pulling in network side-effects.
5. Run the full test suite inside the worktree — must stay green.

## Test plan

- **Existing tests that must stay green**: the whole `tests/` suite (baseline on main: 124 passing). Highest-risk tests are anything touching the summary flow — `test_summary*`, `test_agent*`. I will not grep for these in advance; the full suite will tell me if I broke something.
- **New test added**: `tests/test_summary_request_signature.py::test_summary_request_has_no_dead_log_param` — reads `agent.py` as text, parses with `ast`, asserts:
  1. The `_summary_request` FunctionDef's `args.args` contains no param named `log`.
  2. No `ast.Call` to a `Name('_summary_request')` has a second positional arg whose identifier contains `log`.
- **Re-run probe**: not live-probe-driven this cycle — the metric is a deterministic static scan, no variance, so no P-* re-run is needed. The regression test IS the after-measurement. (Same shape as cycles 0013-dead-locals and 0014-dead-imports.)

## Risks & mitigations

- **Risk**: a caller site passes `log` as the second positional while also passing `base_url`/`model` as kwargs — if I mis-edit the call, the kwarg might shift. **Mitigation**: each edit is a targeted delete of `, log` or `, self._log`; I review each call site's diff individually and run the full test suite to catch any arity/arg-shape break.
- **Risk**: the `log` parameter is secretly used via `locals()['log']` or similar reflection. **Mitigation**: I already confirmed via `ast.unparse` of the body that `log` does not appear as an identifier in the function body. No dynamic access patterns exist in this function.
- **Risk**: a future cycle wants to add logging into `_summary_request` and would have needed the parameter. **Mitigation**: trivially re-add later — one line in the signature, one line per call site. Not worth keeping dead wiring against a hypothetical.

## Rollback

Worktree is isolated. If VERIFY fails after 3 debug iterations, follow the null-result path in Phase 8: `git worktree remove --force`, `git branch -D cicd/0016-summary-request-dead-log`, comment on #36 with failure analysis, null-result row in progress.md, leave #36 open.

## Closes

Closes #36
