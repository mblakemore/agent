# 0026 — config-toplevel-merge — results

- Issue: #56
- Branch: cicd/0026-config-toplevel-merge
- PR: (pending)
- Commit range: c449c37
- Date: 2026-04-11

## Metric

- Baseline: 0 of 2 top-level user config keys (`log_dir`, `log_prefix`) loadable from config.json — both always silently dropped
- After:    2 of 2 — both keys correctly copied into `_config` by `_load_config()`
- Delta:    +2 (+100%) — from 0 working to 2 working

## Test suite

- Before: 140 passing
- After:  144 passing (+4 new in `TestLoadConfigTopLevel`)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0026-pcount-after.log
- Verdict: PASS (2 turns, 1 tool call, correct answer 140 — identical to before baseline)

## What I actually changed

- `agent.py` `_load_config()`: added 4 lines after the existing `for section in config:` loop to copy top-level non-dict keys from `user_config` that are not already in `_DEFAULT_CONFIG`
- `tests/test_default_config.py`: added `TestLoadConfigTopLevel` class with 4 regression tests:
  - `test_log_prefix_override` — verifies `log_prefix` from config.json is loaded
  - `test_log_dir_override` — verifies `log_dir` from config.json is loaded
  - `test_section_override_still_works` — verifies dict-section merging still works
  - `test_unknown_scalar_does_not_overwrite_section` — verifies scalar can't overwrite existing section dict

## What I learned

- The merge loop `for section in config:` is subtly exclusive — it only considers keys in `_DEFAULT_CONFIG` AND only when user_config has a dict value for that key. Any top-level scalar in user config.json is a dead letter.
- The fix is safe because the `key not in config` guard prevents user scalars from overwriting `_DEFAULT_CONFIG` sections.
- `log_dir` and `log_prefix` have no `_DEFAULT_CONFIG` defaults, so `_config.get("log_dir")` returning None and `_config.get("log_prefix", "session")` using the hardcoded fallback were the only ways these ever worked — meaning the config file feature was always broken, silently.
