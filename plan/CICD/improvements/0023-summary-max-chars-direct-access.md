# 0023 — summary-max-chars-direct-access

**Issue**: #50 — bug: _SUMMARY_MAX_CHARS uses stale .get() fallback (1500) instead of direct dict access — _DEFAULT_CONFIG has 3000
**Branch**: cicd/0023-summary-max-chars-direct-access

## Goal

Replace the stale `.get("summary_max_chars", 1500)` call at `agent.py:134` with direct `["summary_max_chars"]` access, matching the access pattern used by every other context config key.

## Motivation

`agent.py:134` reads:

```python
_SUMMARY_MAX_CHARS = _config["context"].get("summary_max_chars", 1500)
```

But `"summary_max_chars"` has been in `_DEFAULT_CONFIG["context"]` at value `3000` since it was added (line 83). The `.get()` fallback of `1500` is a stale pre-refactor artifact that:

1. **Is wrong**: the defined default is `3000`, not `1500`. If a future code path bypassed the config merge, the effective default would silently be halved.
2. **Is inconsistent**: lines 131–133 and 135 all use direct `[]` access for other context keys (`max_full_lines`, `preview_lines`, `summary_threshold`, `max_context_messages`). Line 134 is the sole outlier.
3. **Misleads readers**: the fallback `1500` implies the key might not be present, masking the fact that `_DEFAULT_CONFIG` guarantees it is.

Issue #50.

## Success metric

- Baseline: `grep -c '\.get("summary_max_chars"' agent.py` → **1**
- Target: **0** (direct `[]` access, no inline fallback)
- Measurement method:
  ```bash
  cd /tmp/agent-cicd/0023-summary-max-chars-direct-access
  grep -c '\.get("summary_max_chars"' agent.py
  ```
  Target: `0`

## Scope

- **In**:
  - `agent.py` line 134: change `.get("summary_max_chars", 1500)` → `["summary_max_chars"]`
  - `tests/test_default_config.py`: add two new tests asserting `"summary_max_chars"` is present in `_DEFAULT_CONFIG["context"]` and its value equals `3000`
- **Out**:
  - All other files — `summary_max_chars` has no references outside `agent.py` and the new test
  - README — no documentation change needed; this is an internal consistency fix
  - Any other config keys — out of scope for this cycle

## Implementation steps

1. Edit `agent.py` line 134: replace
   ```python
   _SUMMARY_MAX_CHARS = _config["context"].get("summary_max_chars", 1500)
   ```
   with:
   ```python
   _SUMMARY_MAX_CHARS = _config["context"]["summary_max_chars"]
   ```
2. Add two tests to `tests/test_default_config.py`:
   - `test_summary_max_chars_in_default_config`: asserts `"summary_max_chars"` is in `_DEFAULT_CONFIG["context"]`
   - `test_summary_max_chars_default_value`: asserts `_DEFAULT_CONFIG["context"]["summary_max_chars"] == 3000`
3. Run full test suite, confirm 134 tests pass (+ 2 new = 136).
4. Run success metric: `grep -c '\.get("summary_max_chars"' agent.py` → 0.

## Test plan

- Existing tests that must stay green: full suite (134 passing).
- New tests: 2 assertions in `tests/test_default_config.py` that convert the discoverability gap into a statically caught invariant.
- Re-run metric: grep from "Success metric" — baseline `1`, target `0`.

## Risks & mitigations

- **Risk**: Changing from `.get()` to `[]` could raise `KeyError` if `_DEFAULT_CONFIG` ever loses the key.
  **Mitigation**: The new tests assert the key exists in `_DEFAULT_CONFIG["context"]`. The `_load_config` function deep-merges from `_DEFAULT_CONFIG`, so the key will always be present in `_config["context"]` at module load time.
- **Risk**: A test asserts the exact set of context keys.
  **Mitigation**: `grep -r 'summary_max_chars\|context.*keys' tests/` shows no such assertion exists before implementing.

## Rollback

`git revert <commit>` restores the `.get()` call atomically. No config, no migration, no persistent state.

## Closes #50
