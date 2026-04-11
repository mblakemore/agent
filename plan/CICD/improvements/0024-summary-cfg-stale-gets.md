# 0024 — summary-cfg-stale-gets

**Issue**: #52 — bug: summary config section accessed with stale .get() fallbacks — _DEFAULT_CONFIG guarantees "summary" dict is always present
**Branch**: cicd/0024-summary-cfg-stale-gets (will be created in Phase 6)

## Goal

Replace all stale `_config.get("summary", {})` calls and inner `summary_cfg.get("key", redundant-default)` calls with direct `[]` access, matching the pattern used by every other section of `_DEFAULT_CONFIG` in `agent.py`.

## Motivation

`_DEFAULT_CONFIG` at `agent.py:69–104` defines `"summary"` as a fully-populated dict. `_load_config()` at `agent.py:107–122` deep-copies `_DEFAULT_CONFIG` before merging any user config, guaranteeing `_config["summary"]` is always present and always contains every key defined in `_DEFAULT_CONFIG["summary"]` (`base_url`, `model`, `enabled`, `max_wait_on_save`).

Despite this guarantee, four call sites use `_config.get("summary", {})` — implying the section might be absent — and one uses `summary_cfg.get("base_url", "http://127.0.0.1:8082")` with a fallback that is literally identical to `_DEFAULT_CONFIG["summary"]["base_url"]`. These are the same class of stale defensive `.get()` that cycle 0023 fixed for `summary_max_chars`. Issue #52.

Probe log: `/tmp/agent-cicd/probes/0024-static-grep.log`

## Success metric

- **Metric A** — stale section-level gets: `grep -c '_config.get("summary"' agent.py`
  - Baseline: **4** (lines 563, 659, 764, 1217)
  - Target: **0**
- **Metric B** — stale key-level get with redundant explicit default: `grep -c '.get("base_url", "http://127.0.0.1:8082")' agent.py`
  - Baseline: **1** (line 1219)
  - Target: **0**
- Combined: **5 stale `.get()` calls → 0**
- Measurement method (run inside the worktree):
  ```bash
  A=$(grep -c '_config\.get("summary"' agent.py 2>/dev/null || echo 0)
  B=$(grep -c '.get("base_url", "http://127.0.0.1:8082")' agent.py 2>/dev/null || echo 0)
  echo "A=$A B=$B combined=$((A + B))"
  # Target: A=0 B=0 combined=0
  ```

## Scope

**In**:
- `agent.py`: change the 5 stale `.get()` calls with explicit redundant fallbacks
- `agent.py`: also change the 5 inner `summary_cfg.get("key")` (no fallback) calls on guaranteed-present keys to direct `[]` access — same correctness fix, no metric impact but included for consistency
- `tests/test_default_config.py`: add 2 tests asserting `_DEFAULT_CONFIG["summary"]` has all expected keys and `_load_config()` result supports direct `["summary"]` access

**Out**:
- `_config.get("log_dir")` (line 911) — `log_dir` is genuinely absent from `_DEFAULT_CONFIG`; correctly uses `.get()`
- `_config.get("log_prefix", "session")` (line 919) — same
- All other files — `summary_cfg` is local to functions in `agent.py` only
- README — no documentation change needed; internal consistency fix

## Implementation steps

1. Fix line 563: `_config.get("summary", {})` → `_config["summary"]`
2. Fix line 659: `_config.get("summary", {})` → `_config["summary"]`
3. Fix line 764: `self._config.get("summary", {}).get("max_wait_on_save", 10)` → `self._config["summary"]["max_wait_on_save"]`
4. Fix line 1217: `_config.get("summary", {})` → `_config["summary"]`
5. Fix line 1219: `summary_cfg.get("base_url", "http://127.0.0.1:8082")` → `summary_cfg["base_url"]`
6. Cleanup — fix 5 no-fallback `.get()` calls on guaranteed-present keys:
   - Line 564: `summary_cfg.get("base_url")` → `summary_cfg["base_url"]`
   - Line 565: `summary_cfg.get("model")` → `summary_cfg["model"]`
   - Line 660: `summary_cfg.get("base_url")` → `summary_cfg["base_url"]`
   - Line 663: `summary_cfg.get("enabled")` → `summary_cfg["enabled"]`
   - Line 1218: `summary_cfg.get("enabled")` → `summary_cfg["enabled"]`
7. Add 2 tests to `tests/test_default_config.py`:
   - `test_summary_section_keys_in_default_config`: asserts `_DEFAULT_CONFIG["summary"]` has all 4 keys (`base_url`, `model`, `enabled`, `max_wait_on_save`)
   - `test_load_config_summary_section_always_present`: asserts `_load_config()["summary"]` is a dict and all 4 keys are directly accessible via `[]`
8. Run full test suite (136 → 138 passing)
9. Run success metric — target: A=0, B=0

## Test plan

- Existing tests that must stay green: full suite (136 passing)
- New tests (2): in `tests/test_default_config.py`
  - `test_summary_section_keys_in_default_config`: validates all 4 `_DEFAULT_CONFIG["summary"]` keys are present, converting the "section is always present" assumption into a statically-caught invariant
  - `test_load_config_summary_section_always_present`: runs `_load_config()` from a temp dir (no `config.json`) and confirms `config["summary"]` is directly accessible with `[]` on all 4 keys

## Risks & mitigations

- **Risk**: A user's `config.json` sets `"summary": null` or `"summary": "disabled"`, which would cause a `KeyError` if we drop the `.get()` guard.
  **Mitigation**: The merge logic at `agent.py:118–120` only updates keys when `isinstance(user_config[section], dict)` is true. If the user sets `"summary"` to a non-dict, `config["summary"]` retains the `_DEFAULT_CONFIG` value (the dict). The new test `test_load_config_summary_section_always_present` verifies this from a tempdir without `config.json`. The isinstance guard in `_load_config` already handles the non-dict case.
- **Risk**: Changing `.get("enabled")` to `["enabled"]` breaks code if user sets `"summary"` as a partial dict missing `"enabled"`.
  **Mitigation**: The merge uses `dict.update()`, which only merges provided keys — `"enabled"` from `_DEFAULT_CONFIG["summary"]` remains unless the user explicitly sets `"enabled"` in their `config.json["summary"]`. Since `_DEFAULT_CONFIG` always provides the key, direct access is safe.

## Rollback

`git revert <commit>` restores all `.get()` calls atomically. No config, no migration, no persistent state.

## Closes #52
