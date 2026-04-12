# 0030 — local-import-dedup — results

- Issue: #64
- Branch: cicd/0030-local-import-dedup
- Date: 2026-04-12

## Metric

- **Primary**: `grep -c 'import re as _re\|import hashlib as _hl\|from pathlib import Path as _P' agent.py`
  - Baseline: **4**
  - After:    **0**
  - Delta:    **−4 (−100%)**

## Test suite

- Before: **152** passing
- After:  **153** passing (152 existing + 1 new in `tests/test_no_dead_imports.py`)

## Probe

- Static grep scan (no live LLM required)
- All 4 redundant local import sites confirmed removed
- Module-level `hashlib` confirmed at line 9 of agent.py
- All 4 usage sites confirmed using module-level names directly

## What I actually changed

### `agent.py` (net −4 local import lines, +1 module-level import)

1. Added `import hashlib` to module-level stdlib block (line 9, after `import json`)
2. Removed `import re as _re` from `_auto_increment_cycle()` (~line 1067); replaced `_re.search(...)` with `re.search(...)`
3. Removed `import hashlib as _hl` from `run_agent_single()` hot path (~line 1650); replaced `_hl.md5(...)` with `hashlib.md5(...)`
4. Removed `import re as _re` from `run_agent_single()` hallucination guard try block (~line 1696); replaced `_re.findall(...)` / `_re.IGNORECASE` with `re.findall(...)` / `re.IGNORECASE`
5. Removed `from pathlib import Path as _P` from same inner loop (~line 1702); replaced `_P.cwd()` with `Path.cwd()`

### `tests/test_no_dead_imports.py` (+20 lines)

Added new class `TestNoShadowingLocalImports` with one method:
- `test_no_shadowing_local_imports_in_agent_py` — asserts none of the 3 bad patterns appear in agent.py source text

## What I learned

- **Function-body re-imports of top-level names are easy to spot, hard to justify.** The `_re` and `_P` aliases looked like deliberate namespacing at first glance but were just artifacts of the original author copy-pasting from an isolated context. The module-level bindings are identical.
- **The hot-loop case (hashlib) is the most impactful.** `import hashlib as _hl` at line 1650 ran inside every turn of the `run_agent_single` main loop on text-only responses. Python's import cache means it's a dict lookup rather than a disk read, but re-binding a module name on every loop iteration is pure noise that a linter would flag.
- **Static grep is a valid probe for this class of bug.** No live LLM needed — the measurement is a line count that is deterministic and fast. Same approach as cycles 0013, 0014, 0016, 0018, 0019, 0020, 0023.
- **`test_no_dead_imports.py` is the right home for this guard.** It already guards top-level dead imports (cycle 0014). The new test class guards a related invariant one scope down (local re-imports of global names), keeping related static assertions co-located.
