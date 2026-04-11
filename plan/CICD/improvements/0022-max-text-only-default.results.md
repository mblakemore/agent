# 0022 — max-text-only-default — results

- Issue: #47
- Branch: cicd/0022-max-text-only-default
- PR: #49
- Commit range: ca70c07..5895d4a
- Date: 2026-04-11

## Metric

- Baseline: keys documented in README `cycle` section absent from `_DEFAULT_CONFIG` = **1** (`max_text_only` missing)
- After:    **0** (all three cycle keys present)
- Delta:    −1 (−100%)

## Test suite

- Before: 131 passing
- After:  134 passing (+3 new assertions in `tests/test_default_config.py`)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0022-pcount-after.log
- Turns: 2, tool calls: 1, answer: 131 — PASS (identical to before)

## What I actually changed

- `agent.py`: added `"max_text_only": 3` to `_DEFAULT_CONFIG["cycle"]`
- `agent.py`: hoisted `_MAX_TEXT_ONLY = _config["cycle"]["max_text_only"]` as a module-level constant next to `_MAX_TURNS` / `_WIND_DOWN_TURNS`
- `agent.py`: removed the inline local `_MAX_TEXT_ONLY = _config.get("cycle", {}).get("max_text_only", 3)` inside `run_agent_single` — function body now resolves to the module constant
- `tests/test_default_config.py`: new file — 3 assertions guard the `_DEFAULT_CONFIG["cycle"]` key surface against future README/config divergence

## What I learned

- The pattern of hoisting config reads as module-level constants (`_MAX_TURNS`, `_WIND_DOWN_TURNS`) was not applied consistently to all cycle keys — a sweep after adding new keys is worth doing.
- The `_load_config` shallow-update-per-section merge means any key missing from `_DEFAULT_CONFIG` is silently invisible to user-config overrides; completeness of defaults is load-bearing, not just documentary.
