# 0014 — dead-imports

**Issue**: #28 — bug: dead top-level imports across agent.py, callbacks.py, tools/file.py, tool_recovery.py
**Branch**: cicd/0014-dead-imports

## Goal

Delete the five dead module-level imports identified in #28 and add an AST-based regression guard covering the same four files.

## Motivation

Cycle 0013 (#24) cleaned up five dead **local** assignments using an AST scan. The same scan lifted one scope up revealed five dead **module-level** imports in exactly the same family of files (plus `callbacks.py`). Static grep confirmed zero consumers — no re-exports, no side-effect uses that couldn't be satisfied by existing sibling imports. This is the next layer of the same rot.

- Issue: #28
- Scan approach used to find them: walk `ast.Import` / `ast.ImportFrom`, diff against word-boundary search on the rest of the file source.
- No live LLM probe — cycle 0012 established that grep/AST-measurable defects skip the probe tax.

## Success metric

- **Baseline**: **5** dead top-level imports across the 4 target files, from the AST scan in issue #28:
  - `tool_recovery.py:10: logging`
  - `tools/file.py:3: os`
  - `callbacks.py:20: json`
  - `agent.py:32: _QWEN_TOKENIZER_AVAILABLE`
  - `agent.py:43: _cbmod`
- **Target**: **0**.
- **Measurement method** (deterministic, committed as a script inside the regression test):

```python
import ast, re
from pathlib import Path
for path in ['tool_recovery.py', 'tools/file.py', 'callbacks.py', 'agent.py']:
    src = Path(path).read_text()
    tree = ast.parse(src)
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split('.')[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == '__future__':
                continue  # compile-time directive, not a real name
            for a in node.names:
                if a.name != '*':
                    imported[a.asname or a.name] = node.lineno
    lines = src.splitlines()
    for name, ln in imported.items():
        body = '\n'.join(l for i,l in enumerate(lines,1) if i != ln)
        if not re.search(r'\b' + re.escape(name) + r'\b', body):
            print(f'{path}:{ln}: {name}')
```

After the cycle the command must print nothing.

## Scope

- In:
  - `tool_recovery.py` — delete `import logging` at line 10.
  - `tools/file.py` — delete `import os` at line 3.
  - `callbacks.py` — delete `import json` at line 20.
  - `agent.py` — drop `_QWEN_TOKENIZER_AVAILABLE` from the `from token_utils import ...` at line 32 (leave the rest of the import intact).
  - `agent.py` — delete `import callbacks as _cbmod` at line 43.
  - `tests/test_no_dead_imports.py` (new) — AST guard mirroring `test_no_dead_locals.py` at module scope, whitelisted to the four files touched.
- Out:
  - Repo-wide import hygiene (other files may have dead imports; not in scope).
  - `from __future__ import annotations` lines (the scan explicitly skips `__future__`).
  - The null-result `#21` exploration.
  - Any functional behavior change.

## Implementation steps

1. Baseline scan re-run from the clean worktree → confirm the exact list of 5 names matches what was filed in #28.
2. For each of the 5 names, `grep -rn '\\b<name>\\b' --include='*.py' .` across the whole worktree. Record the counts in the commit messages — zero external consumers is the gate.
3. Delete the five imports in per-file commits:
   - Commit 1: `tools/file.py` — `import os`
   - Commit 2: `tool_recovery.py` — `import logging`
   - Commit 3: `callbacks.py` — `import json`
   - Commit 4: `agent.py` — partial edit on line 32 (drop `_QWEN_TOKENIZER_AVAILABLE` from the import list) + delete line 43.
4. After every commit, run `python3 -m unittest discover tests` and `python3 -c "import agent, callbacks, tool_recovery; import tools.file"`. If either fails, stop and debug before making the next change.
5. Add `tests/test_no_dead_imports.py` — AST scan, whitelisted to the 4 files, skips `from __future__`, asserts the scan's output list is empty. On failure, the assertion message names each offending `file:line: name`. Verify the guard fires by monkey-patching `Path.read_text` to inject a fake offender and confirming the test reports it. Same pattern cycle 0012's `test_tools_no_raw_ansi.py` used.
6. Final commit: the new test + `plan/CICD/improvements/0014-dead-imports.md` + `plan/CICD/improvements/0014-dead-imports.results.md` + progress.md row.

## Test plan

- **Existing tests that must stay green**: all 121 currently passing.
  - `tests/test_no_dead_locals.py` (cycle 0013 guard) — unaffected, operates at local scope.
  - `tests/test_tui.py::test_qwen_tokenizer_*` — these patch `token_utils._QWEN_TOKENIZER_AVAILABLE` directly, not `agent._QWEN_TOKENIZER_AVAILABLE`. Safe.
  - `tests/test_callbacks.py`, `tests/test_commands.py`, `tests/test_tools_paging.py` — each imports `callbacks` directly, not through `agent._cbmod`. Safe.
- **New tests I'll add**:
  - `tests/test_no_dead_imports.py::TestNoDeadImports::test_no_dead_top_level_imports` — the AST scan above, asserting empty output.
- **Re-run probe**: no live LLM probe. Smoke-import instead: `python3 -c "import agent, callbacks, tool_recovery; import tools.file"` must exit 0 with no output.

## Risks & mitigations

- **Risk**: `_QWEN_TOKENIZER_AVAILABLE` is re-imported from `agent` by some file I haven't inspected.
  - **Mitigation**: `grep -rn 'from agent import _QWEN_TOKENIZER_AVAILABLE' --include='*.py' .` → zero hits. `grep -rn 'agent\._QWEN_TOKENIZER_AVAILABLE' --include='*.py' .` → zero. Only `tui.py` consumes it, and it imports from `token_utils` directly. Confirmed no consumer.
- **Risk**: `_cbmod` is referenced via `getattr(sys.modules['agent'], '_cbmod')` or a similar indirect lookup.
  - **Mitigation**: `grep -rn "_cbmod" --include='*.py' .` shows exactly one hit — the import line itself. No string / getattr access.
- **Risk**: `import logging` / `import json` / `import os` had a side-effect purpose.
  - **Mitigation**: these are stdlib modules with no meaningful import side effects. Their modules are already imported elsewhere in the process (e.g. `agent.py` imports `os`, `commands.py` imports `json`), so even a hypothetical side effect already runs.
- **Risk**: the AST guard has a false negative (a name used only inside an f-string that the word-boundary regex still matches).
  - **Mitigation**: acceptable — a false negative means the guard misses rot, not that it introduces rot.
- **Risk**: the AST guard has a false positive on compile-time / re-export cases.
  - **Mitigation**: explicit `__future__` skip. If a legitimate re-export trips the guard later, the fix is to add a whitelist entry beside the file path, which is cheaper than letting dead imports accumulate.

## Rollback

Each deletion is a single-line diff on a separate commit. To revert any one deletion:

```bash
git revert <commit-sha>
```

The new test can be deleted wholesale with no side effects — it only reads the source files it names explicitly.

## Closes

Closes #28
