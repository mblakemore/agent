# CICD Progress Log

Running record of improvement cycles. Each row is one end-to-end loop
(PERCEIVE → PROBE → REFLECT → DECIDE → PLAN → IMPLEMENT → VERIFY → TRACK).

| # | Date | Slug | Issue | PR | Probe | Metric | Before | After | Delta | Verdict | Branch |
|---|------|------|-------|----|-------|--------|--------|-------|-------|---------|--------|
| 0001 | 2026-04-10 | extra-tools-dead-call | #2 | (pending) | startup `agent.py --help` | `grep -c 'Failed to load extra tool'` | 1 | 0 | −1 (−100%) | PASS | cicd/0001-extra-tools-dead-call |
| 0002 | 2026-04-10 | tools-paging | #1 | (pending) | P-count (tests/ count) | `/tools` entries visible when 50 buffered | 20 | 50 | +30 (+150%) | PASS | cicd/0002-tools-paging |
| 0003 | 2026-04-10 | search-files-context | #5 | (pending) | P-enum (safe_cb call sites) | tool-call count on probe | 5 | 2 | −3 (−60%) | PASS | cicd/0003-search-files-context |
| 0004 | 2026-04-11 | file-write-auto-mkdir-advertised | #7 | (pending) | P-impl (word_freq + tests) | tool-call count on probe | 4 | 3 | −1 (−25%) | PASS | cicd/0004-file-write-auto-mkdir-advertised |
| 0005 | 2026-04-11 | tools-docstring-cleanup | #9 | (pending) | P-bug (no-regression only) | stale `SHARED RUNTIME`/`tool-agent/` hits in tools/*.py | 8 | 0 | −8 (−100%) | PASS | cicd/0005-tools-docstring-cleanup |
| 0006 | 2026-04-11 | root-docstring-cleanup | #11 | (pending) | smoke-import (no-regression only) | stale `SHARED RUNTIME`/`tool-agent/` hits in `*.py` repo-wide | 3 | 0 | −3 (−100%) | PASS | cicd/0006-root-docstring-cleanup |
| 0007 | 2026-04-11 | search-files-path-in-header | #13 | (pending) | P-enum from empty tempdir | tool-call count on probe | 4 | 2 | −2 (−50%) | PASS | cicd/0007-search-files-path-in-header |
| 0008 | 2026-04-11 | console-dedup | #15 | (pending) | P-bug (running_mean off-by-one) | `wc -l` of probe log (lines) | 206 | 104 | −102 (−49.5%) | PASS | cicd/0008-console-dedup |
| 0009 | 2026-04-11 | search-files-path-warning | #17 | (pending) | P-enum from empty tempdir | tool-call count on probe | 2 | 1 | −1 (−50%) | PASS | cicd/0009-search-files-path-warning |
