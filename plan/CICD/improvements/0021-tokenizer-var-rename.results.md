# 0021 — tokenizer-var-rename — results

- Issue: #46
- Branch: cicd/0021-tokenizer-var-rename
- PR: (pending)
- Commit range: 51079c0
- Date: 2026-04-11

## Metric
- Baseline: 6 references to `_QWEN_TOKENIZER_AVAILABLE` across token_utils.py, tui.py, tests/test_tui.py
- After:    0 references to `_QWEN_TOKENIZER_AVAILABLE`, 7 references to `_EXACT_TOKENIZER_AVAILABLE`
- Delta:    −6 (−100%) misleading name references eliminated

## Test suite
- Before: 131 passing
- After:  131 passing

## Probe re-run
- Log: /tmp/agent-cicd/probes/0021-P-impl.log
- Verdict: PASS (no regression — probe was P-impl, unrelated to this change)

## What I actually changed
- Renamed `_QWEN_TOKENIZER_AVAILABLE` → `_EXACT_TOKENIZER_AVAILABLE` in token_utils.py (definition + 3 uses)
- Updated import in tui.py
- Updated 2 mock.patch.object targets in tests/test_tui.py
- Fixed stale docstring in `count_tokens()` that said "Qwen tokenizer"

## What I learned
- The Qwen→Gemma migration left more than just dead imports (cycle 0014) — it left naming debris in the surviving references
- `exec_command.py:237` also has a "Qwen" comment about heredoc generation, but that's about model behavior patterns not variable naming — different concern for a different cycle
