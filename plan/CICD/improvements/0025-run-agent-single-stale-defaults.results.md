# 0025 — run-agent-single-stale-defaults — results

- Issue: #54
- Branch: cicd/0025-run-agent-single-stale-defaults
- PR: (draft — see TRACK phase)
- Commit range: aeb1a40..<final>
- Date: 2026-04-11

## Metric

- Baseline: 6 stale-default parameters (temperature=0.7, top_p=0.8, top_k=20, presence_penalty=1.5, max_tokens=4096, ctx_size=32768)
- After:    0 stale-default parameters (all 6 now read from _DEFAULT_CONFIG)
- Delta:    −6 (−100%)

## Test suite

- Before: 138 passing
- After:  140 passing (+2 new regression tests in tests/test_default_config.py)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0025-static-after.log
- Verdict: PASS — all 6 defaults now match _DEFAULT_CONFIG values

## What I actually changed

- `agent.py`: replaced 6 hardcoded literal defaults in `run_agent_single` signature with `_DEFAULT_CONFIG` dict lookups
- `tests/test_default_config.py`: added `TestRunAgentSingleDefaults` class with 2 tests:
  - `test_run_agent_single_defaults_match_generation_config`: pins temperature/top_p/top_k/presence_penalty against config
  - `test_run_agent_single_defaults_match_context_config`: pins max_tokens/ctx_size against config

## What I learned

- The `run_agent_single` signature predates the full config system — the defaults were valid when written and silently became stale as `_DEFAULT_CONFIG` evolved.
- Python's "evaluate defaults at function-definition time" semantics makes `_DEFAULT_CONFIG[...]` expressions safe as defaults, as long as `_DEFAULT_CONFIG` is defined before the function.
- The pattern of `inspect.signature` tests is effective for preventing this class of drift from recurring.
