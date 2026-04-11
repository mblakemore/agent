# 0006 — root-docstring-cleanup — results

- Issue: #11
- Branch: cicd/0006-root-docstring-cleanup
- PR: #<PR> (draft, filled in by Phase 8)
- Commit range: 04849d0..<final>
- Date: 2026-04-11

## Metric

**Primary**: count of tracked `*.py` files (excluding `tests/test_tools_docstrings.py`) matching either `SHARED RUNTIME` or `tool-agent/`.

Measurement:

```bash
grep -rEl 'SHARED RUNTIME|tool-agent/' --include='*.py' . \
  | grep -v '^./tests/test_tools_docstrings.py$' \
  | wc -l
```

| When | Count | Offenders |
|---|---|---|
| Baseline (on `main` @ 0799329) | **3** | `./agent.py`, `./token_utils.py`, `./tool_recovery.py` |
| After cycle 0006 | **0** | — |

Delta: **−3 (−100%)**.

## Test suite

- Before: 110/110 passing
- After:  110/110 passing

No new test methods were added. The two existing methods in
`tests/test_tools_docstrings.py` now cover the whole repo instead of only
`tools/`, which is the point of this cycle — increasing the blast radius of
an already-passing gate, not growing the test count.

Class renamed from `TestToolsDocstringsAreAccurate` to
`TestPythonDocstringsAreAccurate`. Grep confirmed nothing references the old
name outside the file itself (checked before renaming, as called out in the
plan's risks section).

## Probe re-run

- Probe: smoke import of `agent`, `token_utils`, `tool_recovery` from a
  fresh cwd with the repo on `sys.path` (no-regression gate only — module
  docstrings are runtime-inert, so this is belt-and-suspenders coverage for
  any accidental syntax error in the docstring rewrites).
- Log: N/A — too small to warrant a saved log. Reproducer:

  ```bash
  cd /tmp && python3 -c "
  import sys
  sys.path.insert(0, '/tmp/agent-cicd/0006-root-docstring-cleanup')
  import agent, token_utils, tool_recovery
  print('imports OK')
  print('agent.__doc__:', repr(agent.__doc__[:60]))
  "
  ```
- Verdict: **PASS** (imports OK both before and after; `__doc__` correctly
  reflects the new content).

## What I actually changed

- `agent.py` — replaced the 9-line stale docstring block with a 6-line
  accurate one. Kept the shebang. No other edits to the 2000-line file.
- `token_utils.py` — replaced the 9-line stale docstring block with a 5-line
  accurate one. Kept the "Gemma 3 tokenizer, falls back to char-based"
  explanation verbatim.
- `tool_recovery.py` — replaced the 10-line stale docstring block with a
  5-line accurate one. Kept the "only triggers on errors" clarification.
- `tests/test_tools_docstrings.py` — rewrote the walker to start at
  `REPO_ROOT` (the parent of `tests/`) and walk all `*.py` files via
  `rglob`, skipping the test file itself plus `__pycache__` / `.git` /
  virtualenv directory names. Failure messages now report repo-relative
  paths. Class renamed to `TestPythonDocstringsAreAccurate`. Test method
  names preserved for backward compatibility (and because they are still
  accurate).

No refactors. No docstring drift in untouched files. No unrelated cleanup.

## What I learned

- **Cycle scope gaps compound.** Cycle 0005's test scope (`tools/` only)
  directly enabled this cycle's existence — if it had walked the whole repo
  from the start, the three top-level offenders would have failed the test
  and the fix would have happened in 0005. The lesson is that when a
  regression test is pinning a cleanup, the test's walk should be at least
  as wide as the problem class, not just as wide as the current cleanup.
- **Grep against only the directory you just edited is a trap.** The
  baseline grep in cycle 0005 was scoped to `tools/`, which hid the
  top-level copies. Running `grep -rEl '<pattern>' --include='*.py' .`
  (repo-wide, Python-only) is the honest baseline for any doc-rot
  cleanup. Folding that habit into the probe library is worth considering
  next cycle.
- **The `_SKIP_DIR_PARTS` set in the broadened test is a deliberately
  conservative allowlist against walking virtualenvs a contributor might
  drop in the repo root.** If someone ever adds a legitimate directory
  named `venv/` or `env/` that should be walked, they will need to update
  the skiplist — callout left in-file implicitly via the constant name.
- **Committing in green-at-every-commit order matters even for tiny
  cycles.** The natural "write the test first, watch it fail, then fix"
  flow would have committed a red test at commit 1, breaking bisect. I
  inverted the order (fix sources first, then broaden the test) and
  validated the broadened test's signal via grep instead of a red run.
  Both paths give the same confidence; only one is bisect-safe.
