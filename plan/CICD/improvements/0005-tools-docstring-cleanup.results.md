# 0005 — tools-docstring-cleanup — results

- Issue: #9
- Branch: `cicd/0005-tools-docstring-cleanup`
- PR: (opened in Phase 8)
- Date: 2026-04-11

## Metric

**Primary**: count of stale `SHARED RUNTIME` / `tool-agent/` phrases in `tools/*.py`. Measured as:

```bash
grep -rEl 'SHARED RUNTIME|tool-agent/' tools/ --include='*.py' | wc -l
```

| | Before | After |
|---|---|---|
| Files matching `SHARED RUNTIME` | 8 | **0** |
| Files matching `tool-agent/` (`.py` only) | 8 + 1 = 9 (incl. a User-Agent string in `web_fetch.py`) | **0** |
| Combined unique files | 8 | **0** |

- Baseline capture: `grep -rn` listed all 8 module headers + the `web_fetch.py:17` User-Agent line. The User-Agent hit was a non-header occurrence I only spotted once the module headers were fixed — a second read of the metric is what flushed it out.
- Delta: **−8 files (−100%)**, target was 0. **PASS.**

## Test suite

- Before: 108 passing
- After:  **110 passing** (+2 new)

New tests in `tests/test_tools_docstrings.py`:
- `test_no_stale_shared_runtime_banner` — walks `tools/*.py`, asserts the substring `SHARED RUNTIME` is absent. Failure message names the offending file(s).
- `test_no_stale_tool_agent_reference` — same walk, asserts `tool-agent/` is absent. Failure message also calls out the `web_fetch.py` User-Agent, since that was the second-order case I only caught on the second grep.

Both tests use `pathlib.Path(__file__).resolve().parent.parent / "tools"` to find the tools directory, matching the pattern in `tests/test_search_files.py` and `tests/test_file_tool.py`, so the tests work regardless of the cwd at invocation.

## Probe re-run

- Before log: `/tmp/agent-cicd/probes/0005-pbug-before.log`
- After log:  `/tmp/agent-cicd/probes/0005-pbug-after.log`
- Before test log: (n/a — baseline was already green on main)
- After test log:  `/tmp/agent-cicd/probes/0005-tests-after.log` (`Ran 110 tests in 1.860s OK`)
- Verdict: **PASS**

| | Before (main) | After (worktree) |
|---|---|---|
| Tool calls | 5 | 5 |
| Turns | 6 | 6 |
| Wall time | 15.6s | 18.1s |
| Final answer | PASS | PASS |

The probe is a **no-regression** check for this cycle, not a feature metric. Tool-call count and turn count are byte-identical across runs (same 5-step plan: `list → exec → read → write(start=1,end=6) → exec`). The 2.5s wall-time drift is well inside LLM noise at this model size. The cycle did not alter any tool behavior or any tool `definition` dict, so this is the expected outcome — what I wanted to prove is that stripping docstrings from every `tools/*.py` module header did not break module import or tool registration, and it didn't.

## What I actually changed

- **`tools/__init__.py`** — replaced the stale banner with an accurate description of the registry / auto-discovery mechanism that the file actually implements (`MAP_FN`, `tools`, `_discover_tools()`, `load_extra_tools()`). Kept the three-line "Each tool module should export: fn, definition" block because it's the one piece of real scaffolding doc that was already there.
- **`tools/exec_command.py`** — dropped the stale paragraph, kept the real two-paragraph commentary about fresh-shell-per-call semantics and when sessions matter (background processes only). Also replaced a double-space between "directory" and "Compound" with a single space (rewriting the paragraph touched it).
- **`tools/file.py`** — collapsed the five-line header (real first line + four stale lines) down to just the accurate first line. No other content in the module's top docstring was worth keeping.
- **`tools/read_pdf.py`** — dropped the stale middle line, kept the accurate `NOTE:` warning telling the model this tool is for PDFs only and to use `file` action `read` for text files.
- **`tools/search_files.py`** — dropped the stale line and removed the dead `import os` below it (fold-in; no `os.` references anywhere in the module, verified via grep before and after). First-line description stays.
- **`tools/sleep.py`** — collapsed the three-line header down to just the accurate first line.
- **`tools/think.py`** — collapsed the six-line header (1 real + 5 stale) down to just the accurate first line. The stale lines also mentioned "do NOT create symlinks to tool-agent/" — another pointer into a nonexistent directory.
- **`tools/web_fetch.py`** — dropped the stale middle line, kept the real commentary about content being saved to a file with only a summary returned. Also updated the HTTP User-Agent header from `Mozilla/5.0 (compatible; tool-agent/1.0)` to `Mozilla/5.0 (compatible; agent/1.0)` so the repo no longer identifies itself to remote servers by a name it doesn't use. No test mocks the UA string, so nothing else had to move.
- **`tests/test_tools_docstrings.py`** — new 47-line module, two tests pinning the cleanup.

Total diff: `+61 / −37` across 9 files (8 edits + 1 new test).

## What I learned

- **The first run of the metric is not always the complete metric.** My Phase-1 grep used `-l`, which only listed files containing the banner lines, and I happily reported "8 files" and moved on. After the header edits, the same grep returned `1`, not `0`, because `web_fetch.py` had a **second** occurrence of `tool-agent/` (in a User-Agent header) that the first grep had merged under the single-file match. Lesson: when the metric is "absence of a string", run the measurement at both the file-list and the line-count level. A true zero is cheaper than a phantom one that blows up near the finish line.
- **Cleanup cycles need to declare their stopping rule explicitly.** I briefly wondered whether the UA change was in-scope or should be a separate cycle. Anchoring back to the issue — "the repo should not identify as tool-agent" — made it obvious: the issue is about *stale naming across the module*, not about the module header specifically. When the metric is defined over a substring, every occurrence is in-scope by default; narrowing is the exception that needs justification.
- **Regression tests for "absence of a phrase" are cheap insurance.** `test_tools_docstrings.py` is 47 lines, runs in microseconds, and will trip the next time anyone (human or LLM) copies a `tools/*.py` file as a template for a new module and keeps the old header. The cost of the tripwire is zero; the cost of the drift it prevents is measured in reader confusion.
- **Plans lie when the author doesn't read them.** The gap-fill pass caught a "109 tests expected" that should have been 110 (two new tests, not one). This is the second cycle (0003 said something similar) where the gap-fill pass is the difference between a clean plan and a plan that says something wrong. Worth keeping the habit even on cycles that feel trivial.
