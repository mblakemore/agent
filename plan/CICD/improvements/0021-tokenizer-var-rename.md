# 0021 — tokenizer-var-rename

**Issue**: #46 — friction: _QWEN_TOKENIZER_AVAILABLE variable name refers to Qwen but actually tracks Gemma 3 tokenizer
**Branch**: cicd/0021-tokenizer-var-rename (will be created in Phase 6)

## Goal

Rename `_QWEN_TOKENIZER_AVAILABLE` → `_EXACT_TOKENIZER_AVAILABLE` across all source and test files to eliminate a misleading leftover from the Qwen→Gemma migration.

## Motivation

Codebase scan during cycle 0021. `token_utils.py:18` defines `_QWEN_TOKENIZER_AVAILABLE` with comment "kept for backward-compat imports". The module actually loads `unsloth/gemma-3-4b-it` (line 23). The name misleads readers in 3 files. Cycle 0014 already removed the dead import of this name from `agent.py`, confirming no broader consumer exists.

## Success metric

- Baseline: `grep -rc '_QWEN_TOKENIZER_AVAILABLE' token_utils.py tui.py tests/test_tui.py` → **6** hits
- Target: **0** hits of `_QWEN_TOKENIZER_AVAILABLE`; **6** hits of `_EXACT_TOKENIZER_AVAILABLE`
- Measurement method:
  ```bash
  cd /mnt/droid/repos/agent
  old=$(grep -rc '_QWEN_TOKENIZER_AVAILABLE' token_utils.py tui.py tests/test_tui.py | awk -F: '{s+=$2}END{print s}')
  new=$(grep -rc '_EXACT_TOKENIZER_AVAILABLE' token_utils.py tui.py tests/test_tui.py | awk -F: '{s+=$2}END{print s}')
  echo "old=$old new=$new"
  ```
  Target: `old=0 new=6`

## Scope

- **In**:
  - `token_utils.py` — rename definition (line 18) and 2 usage sites (lines 24, 30)
  - `tui.py` — rename import (line 270)
  - `tests/test_tui.py` — rename 2 mock.patch references (lines 209, 221)
  - Remove the stale "kept for backward-compat imports" comment (the backward-compat concern is gone — cycle 0014 removed the only other importer)
- **Out**:
  - `agent.py` — already cleaned by cycle 0014; no reference remains
  - `plan/CICD/improvements/0014-*` — historical docs, leave as-is
  - Any test for the rename itself (the existing test_tui tests already exercise the patched variable; renaming the mock target is sufficient)

## Implementation steps

1. `token_utils.py` — rename `_QWEN_TOKENIZER_AVAILABLE` → `_EXACT_TOKENIZER_AVAILABLE` on lines 18, 24, 30. Update comment on line 18 to remove "kept for backward-compat imports".
2. `tui.py` — rename the import alias on line 270: `from token_utils import _EXACT_TOKENIZER_AVAILABLE as _exact`.
3. `tests/test_tui.py` — update 2 `mock.patch.object` calls on lines 209 and 221 to target `"_EXACT_TOKENIZER_AVAILABLE"`.
4. Run full test suite, confirm 131 passing.
5. Run metric measurement, confirm old=0 new=6.

## Test plan

- Existing tests that must stay green: full suite (131 passing), especially `tests/test_tui.py::TestContextCmd` which patches this variable.
- New tests: none needed — the rename is purely mechanical and the existing tests already exercise both True and False states of the flag.
- Re-run metric: grep command from "Success metric" — baseline `old=6 new=0`, target `old=0 new=6`.

## Risks & mitigations

- **Risk**: External code imports `_QWEN_TOKENIZER_AVAILABLE` from `token_utils`.
  **Mitigation**: Cycle 0014 already confirmed no consumer exists outside `tui.py` and `tests/test_tui.py` via comprehensive grep. The name starts with `_` (private by convention). No backward-compat shim needed.
- **Risk**: Test mock targets break if the string doesn't match exactly.
  **Mitigation**: The test patches `token_utils._QWEN_TOKENIZER_AVAILABLE` via `mock.patch.object(token_utils, "_QWEN_TOKENIZER_AVAILABLE", ...)`. Updating the string to `"_EXACT_TOKENIZER_AVAILABLE"` and verifying tests pass confirms correctness.

## Rollback

`git revert <commit>` restores the old name in all 3 files. No config, no migration, no persistent state.

## Closes

Closes #46
