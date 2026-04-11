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
| 0010 | 2026-04-11 | doc-sync-stale-tui | #19 | (pending) | doc-as-code grep (no-regression only) | stale `--tui` + `/tools` "last 20" refs in README.md/tui.py/agent.py | 6 | 0 | −6 (−100%) | PASS | cicd/0010-doc-sync-stale-tui |
| 0012 | 2026-04-11 | think-theme-bypass | #22 | (pending) | direct `NO_COLOR=1` import snippet (no-regression only) | raw `\033[` literal count in `tools/think.py` | 3 | 0 | −3 (−100%) | PASS | cicd/0012-think-theme-bypass |
| 0013 | 2026-04-11 | summarizer-status-dedup | #26 | (pending) | P-count (tests/ count) | `grep -cE "Async summarizer enabled\|\[summary model online at"` on probe log | 2 | 1 | −1 (−50%) | PASS | cicd/0013-summarizer-status-dedup |
| 0013 | 2026-04-11 | dead-locals | #24 | (pending) | P-bug (no-regression only) | static dead-local scan across `tools/file.py::_resolve_path`, `agent.py::run_agent_single` / `_worker`, `tool_recovery.py::_ask_for_param` | 5 | 0 | −5 (−100%) | PASS | cicd/0013-dead-locals |
| 0014 | 2026-04-11 | continue-none-dedup | #30 | (pending) | P-continue (--continue in empty dir) | `grep -c "no checkpoint found"` on probe log | 2 | 1 | −1 (−50%) | PASS | cicd/0014-continue-none-dedup |
| 0014 | 2026-04-11 | dead-imports | #28 | (pending) | AST scan (no live probe) | dead top-level imports across `tool_recovery.py`, `tools/file.py`, `callbacks.py`, `agent.py` | 5 | 0 | −5 (−100%) | PASS | cicd/0014-dead-imports |
| 0015 | 2026-04-11 | spinner-nontty-dedup | #32 | (pending) | P-count (tests/ count) | `grep -cE '-> [a-z_]+\s+-> [a-z_]+\('` on probe log | 1 | 0 | −1 (−100%) | PASS | cicd/0015-spinner-nontty-dedup |
| 0016 | 2026-04-11 | summary-request-dead-log | #36 | (pending) | AST scan (no live probe) | dead-param score on `_summary_request` (1 sig + 6 call sites) | 7 | 0 | −7 (−100%) | PASS | cicd/0016-summary-request-dead-log |
| 0017 | 2026-04-11 | callbacks-surface-signal | #38 | (pending) | source assertion (no live probe) | threaded-but-discarded args in `TerminalCallbacks.{on_cancelled, on_forced_think, on_text_loop_detected}` output (tokens `streaming` / `exec_command` / `3` / `5` missing) | 4 | 0 | −4 (−100%) | PASS | cicd/0017-callbacks-surface-signal |
| 0018 | 2026-04-11 | callbacks-dead-params | #40 | (pending) | AST scan (no live probe) | dead-param count on `TerminalCallbacks.{on_assistant_text(reasoning), on_context_recovery(auto)}` — both always-literal at the sole call site | 2 | 0 | −2 (−100%) | PASS | cicd/0018-callbacks-dead-params |
