# 0024 — summary-cfg-stale-gets — results

- Issue: #52
- Branch: cicd/0024-summary-cfg-stale-gets
- PR: (pending)
- Commit range: 0cc5e5c..0cc5e5c
- Date: 2026-04-11

## Metric

- Metric A — `grep -c '_config\.get("summary"' agent.py`
  - Baseline: 4
  - After:    0
  - Delta:    −4 (−100%)
- Metric B — `grep -c '.get("base_url", "http://127.0.0.1:8082")' agent.py`
  - Baseline: 1
  - After:    0
  - Delta:    −1 (−100%)
- Combined:
  - Baseline: 5
  - After:    0
  - Delta:    −5 (−100%)

## Test suite

- Before: 136 passing
- After:  138 passing (+2 new regression guards)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0024-tests-after.log
- Verdict: PASS — all 138 tests pass; both metrics at 0

## What I actually changed

- `agent.py:563`: `_config.get("summary", {})` → `_config["summary"]`
- `agent.py:564`: `summary_cfg.get("base_url")` → `summary_cfg["base_url"]`
- `agent.py:565`: `summary_cfg.get("model")` → `summary_cfg["model"]`
- `agent.py:659`: `_config.get("summary", {})` → `_config["summary"]`
- `agent.py:660`: `summary_cfg.get("base_url")` → `summary_cfg["base_url"]`
- `agent.py:663`: `summary_cfg.get("enabled")` → `summary_cfg["enabled"]`
- `agent.py:764`: `self._config.get("summary", {}).get("max_wait_on_save", 10)` → `self._config["summary"]["max_wait_on_save"]`
- `agent.py:1217`: `_config.get("summary", {})` → `_config["summary"]`
- `agent.py:1218`: `summary_cfg.get("enabled")` → `summary_cfg["enabled"]`
- `agent.py:1219`: `summary_cfg.get("base_url", "http://127.0.0.1:8082")` → `summary_cfg["base_url"]`
- `tests/test_default_config.py`: added `TestDefaultConfigSummaryKeys` with 2 tests:
  - `test_summary_section_keys_in_default_config`
  - `test_load_config_summary_section_always_present`

## What I learned

- The stale `.get()` pattern on guaranteed-present `_DEFAULT_CONFIG` sections is a recurring class: cycles 0022, 0023, and now 0024 all fixed variants of the same root cause. Worth checking if the `"retry"`, `"cycle"`, `"generation"`, `"llm"` sections have any similar stale defensive access patterns in a future cycle.
- `_load_config()` uses `isinstance(user_config[section], dict)` guard before merging, which means the DEFAULT value for any section is protected from being overwritten by a non-dict. This makes direct `[]` access safe even if a user writes an invalid config.
- The `drain()` method in `AsyncSummarizer` had a double-stale get — both the outer `.get("summary", {})` and inner `.get("max_wait_on_save", 10)` — yet `AsyncSummarizer.__init__` already uses `self._config["llm"]["model"]` directly on the same object at line 730. The inconsistency was a leftover from early defensive coding before `_DEFAULT_CONFIG` was stabilised.
