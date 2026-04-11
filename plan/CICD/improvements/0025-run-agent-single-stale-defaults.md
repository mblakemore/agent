# 0025 — run-agent-single-stale-defaults

**Issue**: #54 — bug: run_agent_single has 6 stale hardcoded defaults that mismatch _DEFAULT_CONFIG
**Branch**: cicd/0025-run-agent-single-stale-defaults (will be created in Phase 6)

## Goal

Replace the 6 stale hardcoded parameter defaults in `run_agent_single`'s signature with expressions that read from `_DEFAULT_CONFIG`, so the function signature stays in sync with the config system.

## Motivation

`run_agent_single` declares 6 parameter defaults that don't match `_DEFAULT_CONFIG`:

| Parameter | Declared default | _DEFAULT_CONFIG value |
|---|---|---|
| `temperature` | `0.7` | `_DEFAULT_CONFIG["generation"]["temperature"] = 1.0` |
| `top_p` | `0.8` | `_DEFAULT_CONFIG["generation"]["top_p"] = 0.95` |
| `top_k` | `20` | `_DEFAULT_CONFIG["generation"]["top_k"] = 64` |
| `presence_penalty` | `1.5` | `_DEFAULT_CONFIG["generation"]["presence_penalty"] = 0.0` |
| `max_tokens` | `4096` | `_DEFAULT_CONFIG["context"]["max_tokens"] = 16384` |
| `ctx_size` | `32768` | `_DEFAULT_CONFIG["context"]["ctx_size"] = 114688` |

These are stale pre-config-refactor values. All 4 call sites in `run_agent_interactive`
always pass explicit config values, so the defaults never fire in practice. But they
mislead code readers and would produce wrong behavior if anyone called `run_agent_single`
directly without explicit args. Issue #54.

Probe log: `/tmp/agent-cicd/probes/0025-p-count.log`

## Success metric

- **Metric**: count of parameters in `run_agent_single` whose declared default != corresponding `_DEFAULT_CONFIG` value
- **Baseline**: `6` (temperature, top_p, top_k, presence_penalty, max_tokens, ctx_size)
- **Target**: `0`
- **Measurement method**:
  ```bash
  python3 -c "
  import inspect, agent
  sig = inspect.signature(agent.run_agent_single)
  defaults = {k: v.default for k, v in sig.parameters.items() if v.default is not inspect.Parameter.empty}
  gen = agent._DEFAULT_CONFIG['generation']
  ctx = agent._DEFAULT_CONFIG['context']
  mismatches = sum([
      defaults.get('temperature') != gen['temperature'],
      defaults.get('top_p') != gen['top_p'],
      defaults.get('top_k') != gen['top_k'],
      defaults.get('presence_penalty') != gen['presence_penalty'],
      defaults.get('max_tokens') != ctx['max_tokens'],
      defaults.get('ctx_size') != ctx['ctx_size'],
  ])
  print(f'Stale-default param count: {mismatches}')
  "
  ```

## Scope

- **In**:
  - `agent.py` — `run_agent_single` function signature: change 6 parameter defaults
  - `tests/test_default_config.py` — add 2 new tests asserting that `run_agent_single`'s default parameter values match `_DEFAULT_CONFIG`
- **Out**:
  - All other files — the call sites in `run_agent_interactive` already pass explicit values; no change needed there
  - `_DEFAULT_CONFIG` itself — not changing defaults, just syncing the function signature to match
  - README — no documentation change needed; this is an internal consistency fix

## Implementation steps

1. Edit `agent.py` — `run_agent_single` signature, 6 changes:
   - `temperature=0.7` → `temperature=_DEFAULT_CONFIG["generation"]["temperature"]`
   - `top_p=0.8` → `top_p=_DEFAULT_CONFIG["generation"]["top_p"]`
   - `top_k=20` → `top_k=_DEFAULT_CONFIG["generation"]["top_k"]`
   - `presence_penalty=1.5` → `presence_penalty=_DEFAULT_CONFIG["generation"]["presence_penalty"]`
   - `max_tokens=4096` → `max_tokens=_DEFAULT_CONFIG["context"]["max_tokens"]`
   - `ctx_size=32768` → `ctx_size=_DEFAULT_CONFIG["context"]["ctx_size"]`
2. Add 2 tests to `tests/test_default_config.py`:
   - `test_run_agent_single_defaults_match_generation_config`: asserts that
     `temperature`, `top_p`, `top_k`, `presence_penalty` defaults in `run_agent_single`
     match `_DEFAULT_CONFIG["generation"]` values
   - `test_run_agent_single_defaults_match_context_config`: asserts that
     `max_tokens`, `ctx_size` defaults in `run_agent_single` match
     `_DEFAULT_CONFIG["context"]` values
3. Run full test suite, confirm 138 → 140 tests passing
4. Run success metric verification

## Test plan

- Existing tests that must stay green: full suite (138 tests)
- New tests (2): in `tests/test_default_config.py`
  - `test_run_agent_single_defaults_match_generation_config`: reads default values via
    `inspect.signature(agent.run_agent_single)` and asserts they equal the config values
  - `test_run_agent_single_defaults_match_context_config`: same for `max_tokens`/`ctx_size`
- Re-run probe: P-count (no live agent run needed, static check only)

## Risks & mitigations

- **Risk**: Some code or test relies on the specific literal defaults (e.g. `0.7` for temperature).
  **Mitigation**: `grep -rn 'temperature=0\.7\|top_p=0\.8\|top_k=20\|presence_penalty=1\.5\|max_tokens=4096\|ctx_size=32768' tests/` — confirms no test hardcodes these values. The call sites in `run_agent_interactive` always pass explicit values, so they're unaffected.
- **Risk**: Python evaluates default argument expressions at function-definition time. If `_DEFAULT_CONFIG` is not yet defined when `run_agent_single` is defined, this would fail.
  **Mitigation**: `_DEFAULT_CONFIG` is defined at line ~68 (before module-level `_load_config()` call); `run_agent_single` is defined at line ~1414 — well after `_DEFAULT_CONFIG`. No ordering issue.

## Rollback

`git revert <commit>` restores the 6 literal defaults atomically. No config migration, no persistent state.

## Closes #54
