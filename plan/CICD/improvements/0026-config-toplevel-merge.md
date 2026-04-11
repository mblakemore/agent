# 0026 — config-toplevel-merge

**Issue**: #56 — bug: _load_config silently drops top-level non-dict keys from user config (log_dir, log_prefix always ignored)
**Branch**: cicd/0026-config-toplevel-merge (will be created in Phase 6)

## Goal

Fix `_load_config()` so that top-level scalar keys in `config.json` (e.g. `log_dir`, `log_prefix`) are copied into `_config` instead of silently dropped.

## Motivation

`_load_config()` merges user config via:
```python
for section in config:  # only iterates _DEFAULT_CONFIG sections
    if section in user_config and isinstance(user_config[section], dict):
        config[section].update(user_config[section])
```

This only processes keys that are (a) already a section in `_DEFAULT_CONFIG` and (b) dict-valued in user config. Top-level scalar keys like `log_dir` and `log_prefix` are never processed — they're silently swallowed. Issue #56, discovered during cycle 0026 static analysis.

## Success metric

- Baseline: 0 of 2 top-level user config keys (`log_dir`, `log_prefix`) are loaded by `_load_config()` when set in `config.json`
- Target: 2 of 2 top-level user config keys load correctly
- Measurement method:
  ```bash
  python3 -m unittest tests.test_config.TestLoadConfigTopLevel -v
  ```
  New test class fails before fix, passes after. Also confirmed by static count:
  ```bash
  grep -c 'log_prefix\|log_dir' /tmp/agent-cicd/0026-config-toplevel-merge/agent.py
  ```
  (should still be 2+ — no deletions, just the load path fixed)

## Scope

- In: `agent.py` — `_load_config()` function only (lines ~107–124)
- In: `tests/test_config.py` — new `TestLoadConfigTopLevel` class (or add to existing config test class)
- Out: `_DEFAULT_CONFIG` structure, all other code paths, callbacks, tools

## Implementation steps

1. Read `_load_config()` in full to understand the exact merge logic.
2. After the existing `for section in config:` loop, add a pass that copies top-level non-dict, non-section values from `user_config` into `config`:
   ```python
   for key, val in user_config.items():
       if key not in config and not isinstance(val, dict):
           config[key] = val
   ```
3. Check `tests/test_config.py` (or nearest existing config test) for the right place to add tests.
4. Add `TestLoadConfigTopLevel` with two test cases:
   - `test_log_prefix_override`: writes `{"log_prefix": "mytest"}` to a temp config.json, calls `_load_config()`, asserts result contains `"log_prefix": "mytest"`.
   - `test_log_dir_override`: writes `{"log_dir": "my_logs"}` to a temp config.json, calls `_load_config()`, asserts result contains `"log_dir": "my_logs"`.
5. Confirm existing tests still pass.

## Test plan

- Existing tests that must stay green: all 140 in `tests/`
- New tests I'll add:
  - `tests/test_config.py` (or create if absent) — `TestLoadConfigTopLevel`:
    - `test_log_prefix_override` — verifies `log_prefix` from user config is loaded
    - `test_log_dir_override` — verifies `log_dir` from user config is loaded
    - `test_section_override_still_works` — existing dict-section merge still works alongside the new scalar copy
- Re-run probe: P-count (same task), expected same result (PASS, ≤2 turns, ≤1 tool call — no regression)

## Risks & mitigations

- **Risk**: a user config with a top-level key that clashes with an existing `_DEFAULT_CONFIG` section name (e.g. `"llm": "bad"`) could overwrite a dict with a scalar. **Mitigation**: the guard `key not in config` prevents this — we only copy keys that aren't already in `_DEFAULT_CONFIG`.
- **Risk**: test file `test_config.py` might not exist. **Mitigation**: create it if absent; use `tempfile` + `chdir` to isolate the config loading.

## Rollback

Delete the worktree branch `cicd/0026-config-toplevel-merge`. The fix is 3 lines in `_load_config()` and a new test class — reverting is `git revert` on the relevant commit.

## Closes

Closes #56
