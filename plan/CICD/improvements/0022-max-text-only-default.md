# 0022 — max-text-only-default

**Issue**: #47 — friction: max_text_only config key missing from _DEFAULT_CONFIG but documented in README
**Branch**: cicd/0022-max-text-only-default (will be created in Phase 6)

## Goal

Add `"max_text_only": 3` to `_DEFAULT_CONFIG["cycle"]` and align the read site to use direct dict lookup (matching the existing pattern for `max_turns` and `wind_down_turns`).

## Motivation

`agent.py:1434` reads `_config.get("cycle", {}).get("max_text_only", 3)` with an inline fallback. This is inconsistent with how the other two `cycle` keys are consumed (lines 142–143: `_config["cycle"]["max_turns"]` / `_config["cycle"]["wind_down_turns"]`). The README (line 128) documents `max_text_only` as a first-class `cycle` config key, but `_DEFAULT_CONFIG` at lines 87–90 only has `max_turns` and `wind_down_turns`. Anyone inspecting `_DEFAULT_CONFIG` to understand the full configuration surface will not discover `max_text_only`. Issue #47.

## Success metric

- Baseline: `grep -n '"max_text_only"' agent.py | grep -c '_DEFAULT_CONFIG\|"max_turns"\|"wind_down_turns"\|87:\|88:\|89:\|90:'` → conceptually, `_DEFAULT_CONFIG["cycle"]` has **0** entries for `max_text_only`.
  Simpler concrete command:
  ```bash
  python3 -c "
  import sys; sys.path.insert(0, '/mnt/droid/repos/agent')
  import agent
  keys = set(agent._DEFAULT_CONFIG['cycle'].keys())
  missing = {'max_turns','wind_down_turns','max_text_only'} - keys
  print('missing from _DEFAULT_CONFIG[cycle]:', len(missing), missing)
  "
  ```
  Baseline output: `missing from _DEFAULT_CONFIG[cycle]: 1 {'max_text_only'}`
- Target: `missing from _DEFAULT_CONFIG[cycle]: 0 set()`
- Measurement method: same Python one-liner above run inside the worktree.

## Scope

- **In**:
  - `agent.py` line 89: add `"max_text_only": 3` to `_DEFAULT_CONFIG["cycle"]`
  - `agent.py` line 1434: change `.get("cycle", {}).get("max_text_only", 3)` → `_config["cycle"]["max_text_only"]`
- **Out**:
  - README — already accurate
  - Any other file — `max_text_only` has no references outside `agent.py`
  - Tests — no existing test covers this config key directly; the change is value-preserving (same default)

## Implementation steps

1. Edit `_DEFAULT_CONFIG["cycle"]` (around line 87-90) to add `"max_text_only": 3` after `"wind_down_turns": 10`.
2. Edit line 1434 to read `_config["cycle"]["max_text_only"]` directly (no fallback needed once it's in defaults).
3. Optionally: also hoist it as a module-level constant `_MAX_TEXT_ONLY = _config["cycle"]["max_text_only"]` adjacent to `_MAX_TURNS` / `_WIND_DOWN_TURNS` at lines 142–143, and reference `_MAX_TEXT_ONLY` at line 1434 — this eliminates the inline read entirely and mirrors the established pattern. This is the preferred approach.
4. Run full test suite, confirm 131 passing.
5. Run success metric, confirm `missing=0`.

## Test plan

- Existing tests that must stay green: full suite (131 passing).
- New tests: add one test to `tests/test_config.py` (or the most appropriate test file) asserting that `_DEFAULT_CONFIG["cycle"]["max_text_only"]` exists and equals 3. This converts the issue from a runtime-detectable gap to a statically-caught invariant.
- Re-run probe P-count: expect same or better turn count (≤2 turns, 1 tool call, answer 131).

## Risks & mitigations

- **Risk**: Adding a key to `_DEFAULT_CONFIG` could break a test that asserts the exact key set.
  **Mitigation**: `grep -r '_DEFAULT_CONFIG\|cycle.*max_turns\|wind_down' tests/` to verify no such assertion exists before starting.
- **Risk**: `_config["cycle"]["max_text_only"]` at the module-level constant line could raise `KeyError` if someone loads `agent.py` with a custom config that has a `cycle` section but omits `max_text_only`.
  **Mitigation**: The `_load_config` function merges with `_DEFAULT_CONFIG` via deep-copy, so a user config with a partial `cycle` section would override but not replace the whole dict. Verify this in `_load_config`.

## Rollback

`git revert <commit>` in the worktree restores both edit sites atomically.

## Closes

Closes #47
