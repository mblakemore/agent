## 0001 — extra-tools-dead-call

**Issue**: #2 — bug: load_extra_tools points at the same tools/ dir, double-loads every tool and prints a startup warning
**Branch**: `cicd/0001-extra-tools-dead-call` (created in Phase 6)

## Goal

Stop `agent.py` from double-loading the builtin `tools/` package at startup, which also eliminates the spurious `Failed to load extra tool exec_command.py: No module named 'extra_tools'` warning.

## Motivation

Static read of `agent.py:152-155` plus `tools/__init__.py:62-126`:

- `_discover_tools()` in `tools/__init__.py:30-38` is invoked at package import time (`tools/__init__.py:126`). It walks `tools/` via `pkgutil.iter_modules` and loads every submodule correctly as `tools.<name>`, populating `MAP_FN` and `tools`.
- `agent.py:153` then computes `_agent_tools_dir = os.path.join(os.getcwd(), "tools")` and calls `load_extra_tools(_agent_tools_dir)` — pointed at the **same** directory the package already owns.
- `load_extra_tools` re-imports every `.py` file via `spec_from_file_location(f"extra_tools.{stem}", path)` (line 85-86). Every tool module runs a second time. `tools/exec_command.py:23` has `from .file import _accessed_files`, which under the fake parent `extra_tools` resolves to `extra_tools.file` — which doesn't exist — so the `except Exception` on line 118-119 logs `Failed to load extra tool exec_command.py: No module named 'extra_tools'`. This warning appears on **every** invocation, including `agent.py --help` and every `python3 -m unittest discover tests` run. It is also baked into all existing `baseline/*.stdout.log` reference logs.

Probe log: `/tmp/agent-cicd/probes/0001-startup-before.log` shows the warning as the first line of `agent.py --help` output.

Archive grep confirms the dead-code origin: the pattern comes from the old multi-repo layout (`/archive/repos/agent-triad/tool-agent/tools/` + `e0/tools/`). When the repos were unified into a single `tools/` package, the `load_extra_tools` call was kept but no longer has a separate directory to load.

Two reasonable fixes:

1. **Minimal** — delete the `load_extra_tools(CWD/tools)` block in `agent.py` entirely. The capability is preserved as an unused helper in `tools/__init__.py` for whenever someone wants to wire it up against a different directory.
2. **Feature-preserving** — repoint `_agent_tools_dir` at `.agent/tools/` so an agent operator can drop bespoke tools next to their session state without editing the shared package.

I'm going with **option 2** (.agent/tools/). `.agent/` is already the conventional state/history dir and creating `.agent/tools/` is optional — if it doesn't exist, `load_extra_tools` no-ops (early return at `tools/__init__.py:73-74`). This kills the warning, preserves the feature, and places extra-tools exactly where they belong in the existing repo conventions.

## Success metric

- **Measurement command**: `python3 agent.py --help 2>&1 | grep -c 'Failed to load extra tool'`
- **Baseline**: 1 (see `/tmp/agent-cicd/probes/0001-startup-before.log`)
- **Target**: 0

Secondary (not gating but reported):
- `python3 -m unittest discover tests 2>&1 | grep -c 'Failed to load extra tool'` — baseline 1 → target 0
- Full suite still 84/84 passing

## Scope

**In**:
- `agent.py` — change `_agent_tools_dir` to `.agent/tools/` (relative to CWD)
- `tests/test_load_extra_tools.py` — new file with a regression test that imports `agent` / runs the startup code and asserts the warning is not emitted

**Out**:
- Any refactor of `tools/__init__.py`'s discovery machinery (it works correctly as-is)
- Any change to `tools/exec_command.py` or its relative import
- Rewriting the `baseline/*.log` snapshots (they're archived artifacts of prior runs; refreshing them is a separate decision)
- Issue #1 (/tools pagination) — remains open for a future cycle

## Implementation steps

1. Read `agent.py:150-160` in the worktree to confirm the exact lines.
2. Change line 153 from `os.path.join(os.getcwd(), "tools")` to `os.path.join(os.getcwd(), ".agent", "tools")`. The surrounding `if os.path.isdir(...)` guard already makes it safe when the dir is absent.
3. Run `python3 agent.py --help 2>&1 | head` and confirm zero warnings.
4. Run `python3 -m unittest discover tests 2>&1 | tail` and confirm 84/84, zero warnings.
5. Write `tests/test_load_extra_tools.py` with two regression tests:
   - **`test_agent_help_emits_no_extra_tool_warning`** — subprocess-runs `python3 agent.py --help` from a clean temp cwd that has no `.agent/tools/` directory, captures combined stdout+stderr, and asserts the substring `Failed to load extra tool` does not appear. This is the exact baseline probe, wired as a test. Pins the regression at the integration layer.
   - **`test_load_extra_tools_loads_from_temp_dir`** — unit-level: creates a `tempfile.TemporaryDirectory`, writes a minimal extra-tool file (`fn` + `definition` with a unique name like `cicd_probe_echo`), calls `tools.load_extra_tools(tmp_dir)`, and asserts the tool name is in `tools.MAP_FN` afterward. Cleans up by deleting the entry from `MAP_FN` and `tools.tools` in a `try/finally`. This confirms the helper still works when pointed at a real separate directory.
6. Re-run the full test suite to confirm both new tests pass and no existing test regresses.

**Test placement notes** (critical — drafted this wrong the first pass):

- The bug only reproduces when `os.getcwd()` contains a `tools/` directory **and** that directory is the same Python package already registered via `tools/__init__.py`. A `tempfile.TemporaryDirectory` doesn't reproduce it because no `tools/` subdir exists.
- Correct integration test: run the subprocess with `cwd=REPO_ROOT` (computed as the parent directory of the test file) and assert `'Failed to load extra tool'` is absent from combined stdout+stderr. This is the condition the probe captured and is what the fix has to hold green.
- Use `sys.executable` and `[sys.executable, os.path.join(repo_root, "agent.py"), "--help"]` so the test doesn't depend on `python3` being on PATH or on any shell expansion.
- The subprocess inherits no environment variables that would affect tool loading; pass `env=os.environ.copy()` for clarity.
- The subprocess test imports nothing from `agent.py` directly, avoiding heavy import side effects during test collection.
- The unit test uses `tools.load_extra_tools` directly on a `tempfile.TemporaryDirectory`, so it does not depend on the `agent.py` fix being correct — it documents the helper's contract.
- Both tests must clean up any `tools.MAP_FN` / `tools.tools` entries they added in a `try/finally` so they don't leak into later tests in the same process.

## Test plan

- **Existing tests that must stay green**: all 84 — `tests/test_callbacks.py`, `tests/test_cancel.py`, `tests/test_commands.py`, `tests/test_file_refs.py`, `tests/test_spinner.py`, `tests/test_tui.py`.
- **New tests**:
  - `test_load_extra_tools.py::test_clean_import_no_warning` — ensures `import tools` produces zero `Failed to load extra tool` warnings under a cwd with no `.agent/tools` present.
  - `test_load_extra_tools.py::test_extra_tool_discovered_from_agent_dir` — creates a temp directory with a minimal extra tool and asserts `load_extra_tools` registers it into `MAP_FN`.
- **Re-run probe**: `python3 agent.py --help 2>&1 | grep -c 'Failed to load extra tool'` — expected delta: 1 → 0.

## Risks & mitigations

- **Risk**: some agent user relies on the undocumented current behavior of dropping a file into `tools/` and having it auto-registered. **Mitigation**: the current behavior is _already_ that every file in `tools/` loads via `_discover_tools()` at import time. Removing the second load changes nothing for files that live in the package — they still load once, correctly. The only behavioral change is for files that previously _only_ loaded the second time (none observed in this repo).
- **Risk**: tests that import `tools` or `agent` and expect certain warning lines in captured logs. **Mitigation**: grep the test suite for `Failed to load extra tool` before committing (expect zero matches outside `baseline/`).
- **Risk**: `.agent/tools/` collides with a future feature using that path. **Mitigation**: the path is a natural sibling of `.agent/history/` and `.agent/state/`, which is the established convention; low collision risk. The directory is created only if the user explicitly makes it.

## Rollback

One-line revert: `git revert <commit>` on the branch, or manually restore `_agent_tools_dir = os.path.join(os.getcwd(), "tools")` in `agent.py:153`. The new test file can be deleted. No data migrations.

## Closes

Closes #2
