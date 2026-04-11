# 0014 — dead-imports — results

- Issue: #28
- Branch: cicd/0014-dead-imports
- PR: (pending)
- Commit range: baf7464..HEAD
- Date: 2026-04-11

## Metric

- **Primary**: AST-derived dead top-level imports across `tool_recovery.py`, `tools/file.py`, `callbacks.py`, `agent.py`
  - Baseline: **5** (the five listed in #28 — `logging`, `os`, `json`, `_QWEN_TOKENIZER_AVAILABLE`, `_cbmod`)
  - After:    **0**
  - Delta:    **−5 (−100%)**
- **Measurement command**: the same deterministic AST+regex scan committed as `tests/test_no_dead_imports.py::TestNoDeadTopLevelImports::test_no_dead_imports_in_guarded_files`.

## Test suite

- Before: **121** passing
- After:  **122** passing (121 existing + 1 new regression guard `tests/test_no_dead_imports.py`)

## Probe re-run

- No live LLM probe — this is a deterministic repo-state defect, following cycle 0012's learning ("grep-metric cycles skip the probe tax").
- Smoke-import check: `python3 -c "import agent, callbacks, tool_recovery; import tools.file"` → exits 0 with no output.
- Full test suite: 122/122 passing on the worktree tip.

## What I actually changed

- `tools/file.py` (net −1 line): removed `import os`. No pathlib- or path-string code inside the file references `os` anywhere.
- `tool_recovery.py` (net −1 line): removed `import logging`. Diagnostics go through `print()` + `_llm_request`.
- `callbacks.py` (net −1 line): removed `import json`. String-format paths handle all rendering.
- `agent.py` (net −2 lines, +1 line for the trimmed import line):
  - Dropped `_QWEN_TOKENIZER_AVAILABLE` from `from token_utils import count_tokens_from_message, count_tools_tokens, _QWEN_TOKENIZER_AVAILABLE`. The name was never referenced inside `agent.py`; `tui.py:270` already imports it directly from `token_utils`.
  - Deleted `import callbacks as _cbmod` (the only `_cbmod` token in the file was the import line itself). The names that agent.py actually uses — `NullCallbacks`, `TerminalCallbacks`, `safe_cb` — come from the adjacent `from callbacks import ...` on the next line.
- `tests/test_no_dead_imports.py` (new file, 87 lines): one `TestNoDeadTopLevelImports` class, one test method. Walks each guarded file with `ast.iter_child_nodes`, collects every top-level `Import`/`ImportFrom` name, and asserts each name appears as a word somewhere else in the source. Skips `from __future__` imports. On failure, the assertion message names each offending `file:line: name`. Negative case verified by monkey-patching `Path.read_text` to re-inject `import os` into `tools/file.py` and confirming the scan reports `[(1, 'os')]`.

Five commits on the branch, one per logical change:

1. `baf7464` — `tools/file.py` `import os`
2. `f462886` — `tool_recovery.py` `import logging`
3. `2c30289` — `callbacks.py` `import json`
4. `28b6ec1` — `agent.py` `_QWEN_TOKENIZER_AVAILABLE` + `_cbmod`
5. final — regression guard + plan + results + progress row

## What I learned

- **Import-scope rot sits exactly where local-scope rot sat.** Cycles 0013 (dead locals) and 0014 (dead imports) landed on the same files (`tools/file.py`, `agent.py`, `tool_recovery.py`) — when a file accumulates dead *locals* from rewrites, it almost always has dead *imports* from the same rewrites. A "find rot one scope up" pass after every scope-N cleanup is cheap and productive.
- **AST + word-boundary-regex is a good enough unused-import detector for a committed guard.** A full flow-sensitive analysis (like pyflakes) would catch more cases but would also be a new runtime dependency. The guard here is 87 lines of stdlib Python and catches the *kind* of regression the cycle was designed to prevent, which is what guards are for. Expanding the file list is cheaper than adding a dependency.
- **Per-file commits pay off when the metric is multi-file.** Four separate commits (one per file) let the test suite run green four separate times, so any future bisect lands on the exact file that caused a regression. If it had all been one commit, the bisect would just say "cycle 0014 broke it."
- **"Import logging" is an easy lie to leave behind.** Three stdlib imports — `os`, `json`, `logging` — in three different files all got there the same way: the author removed the code that used them but never re-read the import block. A `__future__`-style lint trap for stdlib imports specifically would catch 60% of the drift in this category with almost no false positives. Worth considering as a future cycle scope.
