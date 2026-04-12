# 0032 — build-context-footnote-dedup — results

- Issue: #68
- Branch: cicd/0032-build-context-footnote-dedup
- PR: (pending)
- Commit range: 2b0bb50..HEAD
- Date: 2026-04-12

## Metric

- Baseline: **2** `parts = []` blocks inside `_build_context` in `agent.py`
- After:    **0** `parts = []` blocks inside `_build_context` in `agent.py`
- Delta:    **−2 (−100%)**
- Measurement: `python3 -c "import re; f=open('agent.py'); s=f.read(); m=re.search(r'def _build_context\b.*?(?=\ndef [a-zA-Z_]|\Z)', s, re.DOTALL); print(m.group(0).count('parts = []'))"`

Secondary:
- `TOOL RULE` occurrences in `agent.py`: **1 → 2** (definition line + content string; now both paths benefit because the single helper includes it)
- `_build_context_footnote` call sites in `_build_context`: **0 → 2**

## Test suite

- Before: **153** passing
- After:  **162** passing (+9 new in `tests/test_build_context_footnote.py`)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0032-probe-before.log (baseline static grep)
- Post-fix static check: `parts = []` count = 0, TOOL RULE count = 2
- Verdict: **PASS**

## What I actually changed

- `agent.py`: added `_build_context_footnote(summary_text, initial_files)` helper (26 lines) above `_build_context`; replaced the two inline `parts = []` footnote-build blocks (lines 810-822 and 833-843) with two single-line calls to `_build_context_footnote`. Net −21 lines inside `_build_context`.
- `tests/test_build_context_footnote.py`: new file, 158 lines, 9 test methods covering:
  1. TOOL RULE present in helper output
  2. `exec_command` mentioned in TOOL RULE
  3. `heredoc` mentioned in TOOL RULE
  4. `initial_files` included when provided
  5. `None` not stringified when `initial_files=None`
  6. Summary text included
  7. `role='user'` in returned dict
  8. IMPORTANT+working-directory preamble present
  9. Static: `_build_context` body has zero `parts = []` blocks
  10. Static: `_build_context` calls `_build_context_footnote`

## What I learned

- **The condensed path is the dangerous path.** When a session runs long enough to trigger summary condensation, it's already a stressed context — exactly when the agent most needs reliable instructions. Dropping the TOOL RULE there was silent and would only manifest as a regression in very long sessions. This class of "the second code path that runs under pressure has a silent omission" is worth a standing scan: any time a function builds a string in multiple branches, check that every branch includes all the guidance.
- **Extracting a helper reveals the drift instantly.** Once the two blocks were side by side in the review, the missing TOOL RULE was obvious. The duplication was masking the inconsistency.
- **Static test guards are cheap and permanent.** The `test_no_duplicate_parts_build_in_build_context` test will catch any future regression where someone re-inlines the parts build — even if they don't read the cycle notes.
