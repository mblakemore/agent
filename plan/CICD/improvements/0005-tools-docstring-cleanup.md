# 0005 — tools-docstring-cleanup

**Issue**: #9 — friction: tools/\*.py docstring lies — says SHARED RUNTIME DO NOT MODIFY from a tool-agent/ dir that no longer exists
**Branch**: `cicd/0005-tools-docstring-cleanup` (will be created in Phase 6)

## Goal

Replace the stale `SHARED RUNTIME — DO NOT MODIFY. This file is part of tool-agent/ …` header in all 8 `tools/*.py` files with an accurate one-liner describing what the module actually is, and pin the fix with a grep-based regression test so the lie cannot silently come back. Fold in removing the dead `import os` from `tools/search_files.py` since we are already editing that file.

## Motivation

- Friction log in `plan/CICD/agent.md` names this exact cleanup as "Worth a cycle" and even proposes the metric.
- Issue #9 documents the concrete defect and reproduction (`grep -l 'SHARED RUNTIME' tools/*.py` → 8 files).
- Three prior CICD cycles (0001/0003/0004) have already modified files that the banner says "DO NOT MODIFY". The warning is factually wrong *and* has been repeatedly violated without consequence, which erodes trust in any future in-tree warning.
- The banner also references a `tool-agent/` directory that does not exist in this repo. Any LLM or human following that pointer hits a dead end.
- Baseline probe `/tmp/agent-cicd/probes/0005-pbug-before.log` is PASS (5 tool calls, 6 turns, 15.5s wall, tests green) — nothing behavior-breaking to tackle, so documentation correctness in the primary toolbox directory is the highest-leverage tractable win available.

## Success metric

**Primary** (count of stale references under `tools/`):

- Baseline: 8 files match `SHARED RUNTIME`, 8 files match `tool-agent/` (verified in Phase 1).
- Target: **0 matches** for either phrase under `tools/`.
- Measurement method (exact command, run from repo root):

    ```bash
    grep -rEl 'SHARED RUNTIME|tool-agent/' tools/ | wc -l
    ```

    Must print `0`.

**Secondary** (regression guard):

- A new test module `tests/test_tools_docstrings.py` with two tests (one per phrase). Running the full suite with the new module produces `110` passing (up from `108`, `+2`) and trips immediately if either phrase reappears anywhere in `tools/`.

**Tertiary** (fold-in cleanup):

- `tools/search_files.py` loses its unused `import os` line. Proven by the module still importing cleanly, the unit tests for `search_files` still passing, and a `grep -n '^import os' tools/search_files.py` returning nothing.

## Scope

**In**:

- `tools/__init__.py`
- `tools/exec_command.py`
- `tools/file.py`
- `tools/read_pdf.py`
- `tools/search_files.py` (also remove the `import os` line)
- `tools/sleep.py`
- `tools/think.py`
- `tools/web_fetch.py`
- `tests/test_tools_docstrings.py` (new — regression guard)
- `plan/CICD/improvements/0005-tools-docstring-cleanup.md` (this file)
- `plan/CICD/improvements/0005-tools-docstring-cleanup.results.md` (Phase 8)
- `plan/CICD/progress.md` (Phase 8 — append row)

**Out**:

- Any behavior change inside any tool. This cycle edits comments/docstrings only, plus one dead import removal. No fn/definition/return-value changes.
- Touching files under `tests/` other than the new one. The 108 existing tests must pass unchanged.
- Rewriting or restructuring the per-file docstrings beyond the stale-banner replacement. Keep each file's existing real commentary (e.g. `file.py`'s "Unified file operations tool — read, write, insert, append, delete, list." first line) and only strip the lying paragraph after it.
- Updating `plan/CICD/agent.md` to remove the friction-log entry. That is a separate maintenance task — if removed in this cycle it couples two things. (Noted as follow-up, not done here.)

## Implementation steps

1. **Capture baseline** inside the worktree:

    ```bash
    grep -rEl 'SHARED RUNTIME|tool-agent/' tools/ | sort > /tmp/agent-cicd/probes/0005-docstring-baseline.txt
    wc -l < /tmp/agent-cicd/probes/0005-docstring-baseline.txt   # must be 8
    ```

2. **Read each of the 8 `tools/*.py` files** (required by the file tool's read-before-write rule — the plan *is* going to touch real files, so the worktree's agent session will follow the same rule as any model) and note each file's existing "real" first-line description. Some files (e.g. `file.py`, `search_files.py`) already have a legit one-line summary as the first line of the module docstring; only the paragraph after it is stale.

3. **Rewrite each file's header** to a single accurate line of the form:

    ```python
    """<one accurate description of what this module provides>.

    Registered via tools.MAP_FN[...]; see tools/__init__.py for the registry.
    """
    ```

    Concretely (target text per file):

    - `tools/__init__.py` — "Tool registry for the agent. Registers each tool's `fn` and OpenAI-schema `definition` into `MAP_FN` and `TOOLS` so `agent.py` can dispatch by name."
    - `tools/exec_command.py` — "Shell execution tool. Runs a command in a persistent background session and returns stdout/stderr with an exit code."
    - `tools/file.py` — keep existing `"Unified file operations tool — read, write, insert, append, delete, list."` first line; strip the stale paragraph; no second sentence needed.
    - `tools/read_pdf.py` — "PDF reading tool. Extracts text from a PDF file on disk and returns it as a single string."
    - `tools/search_files.py` — keep existing `"Recursive file content search tool."` first line (if present) or add one; strip the stale paragraph.
    - `tools/sleep.py` — "Sleep tool. Pauses the agent for a bounded number of seconds; mostly used to wait on external conditions."
    - `tools/think.py` — "Think tool. Internal scratchpad — lets the agent reason without emitting user-visible output."
    - `tools/web_fetch.py` — "Web fetch tool. Retrieves a URL and returns body text (with optional content-type filtering)."

    If a file's actual purpose differs from my guess above, I will correct it while reading the file, not blindly paste the template. The descriptions must be accurate to the code that exists, not to what I assume the code does.

4. **Remove `import os`** from `tools/search_files.py` in the same commit as the docstring edit for that file. Verify nothing else in the file references `os.` (already confirmed in Phase 1 via grep — no matches).

5. **Write `tests/test_tools_docstrings.py`** with two tests:

    - `test_no_stale_shared_runtime_banner` — walks `tools/*.py`, reads each file as text, asserts the substring `SHARED RUNTIME` is not present in any of them. Failure message names the offending file.
    - `test_no_stale_tool_agent_reference` — same walk, asserts `tool-agent/` is not present. Failure message names the offending file.

    Implementation detail: use `pathlib.Path(__file__).resolve().parent.parent / "tools"` to find the `tools/` directory regardless of cwd, matching the pattern used in the existing `tests/test_search_files.py` and `tests/test_file_tool.py` suites.

6. **Run the full suite** from the worktree root: `python3 -m unittest discover tests`. Expect `Ran 110 tests ... OK`.

7. **Re-verify the primary metric**:

    ```bash
    grep -rEl 'SHARED RUNTIME|tool-agent/' tools/ | wc -l   # must print 0
    ```

8. **Re-run the P-bug probe** from Phase 2 against the worktree's `agent.py` (not main's) — not because this cycle changes agent behavior, but because the loop requires a "before → after" probe re-run on every cycle and it is the cheapest way to confirm we did not accidentally break tool loading by editing 8 module headers.

9. **Commit in 3 logical chunks**:

    - `CICD 0005 (#9): replace stale SHARED RUNTIME docstring across tools/` — all 8 module edits in one commit, since they are the same mechanical change.
    - `CICD 0005 (#9): regression test — no stale tool-agent/ or SHARED RUNTIME phrases` — the new test file.
    - `CICD 0005 (#9): drop dead import os from tools/search_files.py` — the fold-in, separate so the diff is reviewable on its own.
    - Final commit in Phase 8 adds plan + results + progress row and carries the `Closes #9` trailer.

## Test plan

- **Existing tests that must stay green**: all 108 currently passing. Full `python3 -m unittest discover tests` run end-to-end. Especially relevant:
  - `tests/test_file_tool.py` — imports `tools.file`, so a broken docstring that causes the module to fail import would trip it.
  - `tests/test_search_files.py` — imports `tools.search_files`, guards both the `context` feature *and* the module's importability; removing `import os` must not break it.
  - Any agent-level test that imports the registry via `tools.MAP_FN` — would trip if `tools/__init__.py`'s header edit breaks module import (e.g. by stranding an unterminated string).
- **New tests I'll add**:
  - `tests/test_tools_docstrings.py::test_no_stale_shared_runtime_banner` — covers the primary metric as a runtime assertion.
  - `tests/test_tools_docstrings.py::test_no_stale_tool_agent_reference` — covers the related lie about a `tool-agent/` dir.
- **Re-run probe**: P-bug (`/tmp/probe-0005/` with the max_of bug). Expected delta: essentially zero — same 5 tool calls, same PASS. This probe is a no-regression check, not a feature metric. If tool-call count changes by more than ±1 I will investigate whether a tool definition silently shifted.

## Risks & mitigations

- **Risk**: a module's existing docstring is actually *more* than the stale banner, and I strip real content along with the lie.
  **Mitigation**: read each file top-to-bottom before editing. The edit must only touch lines that are part of the stale paragraph, not any real commentary, license header, or imports.

- **Risk**: a module docstring triple-quoted string gets broken (e.g. unclosed quote) and the module fails to import, cascading every test that imports a `tools.*` module.
  **Mitigation**: after each edit, `python3 -c "import tools.<name>"` in the worktree before moving to the next file. Also, running the full suite at the end would catch it, but catching it per-file is cheaper to debug.

- **Risk**: removing `import os` from `search_files.py` breaks an implicit dependency I didn't see (e.g. a test that monkeypatches `tools.search_files.os`).
  **Mitigation**: grep before removing. Already done in Phase 1 — no references to `os.` in the module and no references to `search_files.os` anywhere under `tests/`. Still, re-grep once more inside the worktree before committing the fold-in.

- **Risk**: the new regression test is too strict and fires on legitimate mentions elsewhere — e.g. if a future file comment legitimately references the historical `tool-agent/` layout.
  **Mitigation**: scope the test to `tools/*.py` only, not the whole repo. The friction-log entry in `plan/CICD/agent.md` *does* mention `tool-agent/` and must stay legal.

- **Risk**: someone reading `tools/__init__.py`'s new docstring expects it to reference the other tool modules by name and the minimal one-liner leaves them uncertain.
  **Mitigation**: the new header describes the purpose (registry) and points to the file itself as the source of truth for the registry contents — no need to enumerate, the code is right there.

## Rollback

Clean rollback is trivial: this cycle only touches comments/docstrings (+ one unused import + one new test file). If verification fails irrecoverably:

```bash
cd /tmp/agent-cicd/0005-tools-docstring-cleanup
git reset --hard <first commit hash on this branch>^
```

or, if the worktree needs to be wiped:

```bash
cd /mnt/droid/repos/agent
git worktree remove /tmp/agent-cicd/0005-tools-docstring-cleanup --force
git branch -D cicd/0005-tools-docstring-cleanup
```

The parent `main` checkout is never touched, so rollback has no effect on the primary working tree regardless.

## Closes

Closes #9
