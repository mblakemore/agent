# 0016 — summary-request-dead-log — results

- Issue: #36
- Branch: cicd/0016-summary-request-dead-log
- PR: (draft, to be opened in Phase 8)
- Commit range: (this cycle's commits on `cicd/0016-summary-request-dead-log`)
- Date: 2026-04-11

## Metric

- Baseline: **7**  (`_summary_request` has 1 dead `log` parameter + 6 call sites passing a dead `log`/`self._log` positional)
- After:    **0**  (parameter dropped, all 6 call sites cleaned)
- Delta:    **−7 (−100%)**
- Measurement: the exact AST scan recorded in the plan (`plan/CICD/improvements/0016-summary-request-dead-log.md`).

## Test suite

- Before: **124** passing (`main @ b4ed18b`)
- After:  **126** passing (124 unchanged + 2 new in `tests/test_summary_request_signature.py`)

Full run: `/tmp/agent-cicd/probes/0016-tests-after.log` — `Ran 126 tests in 1.930s  OK`.

## Probe re-run

Not applicable this cycle — metric is a deterministic AST scan with zero variance, not a live-probe metric. Same pattern as cycles 0013-dead-locals and 0014-dead-imports. The two new regression tests *are* the after-measurement.

## What I actually changed

- `agent.py:553` — dropped `log` from `_summary_request`'s signature.
- `agent.py:598` — `_summary_request(prompt, log)` → `_summary_request(prompt)` inside `_condense_summary`.
- `agent.py:662` — same cleanup inside the sync summary try-block.
- `agent.py:664-665` — dropped `log` positional from the fallback call (kwargs `base_url` / `model` preserved).
- `agent.py:672-673` — same cleanup inside the `ConnectionError`/`Timeout` fallback.
- `agent.py:721` — `_summary_request(prompt, self._log)` → `_summary_request(prompt)` inside `AsyncSummarizer._worker`.
- `agent.py:725-729` — same cleanup on the async fallback call.
- `tests/test_summary_request_signature.py` (new) — two AST-based regression tests that fail if either the signature or any call site re-introduces a dead `log` positional.

`_condense_summary(text, log=None)` — out of scope, untouched. Its `log` parameter IS used (lines 587-588, 601-605, 609-610) and is passed in by legitimate callers.

## What I learned

- The dead-wiring family (cycles 0013 dead-locals, 0014 dead-imports, 0016 dead-parameter) is still paying out in this repo — all three were leftover from the same pre-cleanup era and each had a clean static-scan metric with zero variance. Future cycles should keep a static-scan sweep of the parent checkout as a cheap first move when the open issue queue looks thin.
- An `ast.Name` vs `ast.Attribute` distinction matters when scanning call sites for "passes a log identifier": three of the six offending calls in this cycle were `self._log` attribute accesses, which a naïve `ast.Name`-only scan would miss. Baseline I first eyeballed from `grep` was 5; the AST scan caught the real 6. Grep-then-AST is a good sanity pair for dead-call audits.
- The paired issue body for this cycle (`#36`) was written before I ran the AST scan and understated the call count as 5. That didn't affect the fix — the plan used the AST-derived 7 score — but future cycles should run the measurement script before writing the issue body, not after. File the issue with the real baseline from the get-go.
