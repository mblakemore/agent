# CICD Review Log

Running record of PR review decisions. Each row is one end-to-end review cycle
(PERCEIVE → SELECT → READ → VERIFY → ASSESS → ACT → TRACK).

| R-# | Date | PR | Issue | Verdict | Metric? | Tests | Reason |
|-----|------|----|----|---------|---------|-------|--------|
| R-0001 | 2026-04-11 | — | — | NO_TARGET | n/a | 131/131 green | No open PRs in queue; all CICD PRs through #45 (cycle 0020) already merged |
| R-0002 | 2026-04-11 | #48 | #46 | MERGE | yes — old=0 new=7 (plan targeted 6; extra hit is docstring in count_tokens(), correct and in-scope) | 131/131 | All claims hold; renamed _QWEN_TOKENIZER_AVAILABLE → _EXACT_TOKENIZER_AVAILABLE across 3 files |
| R-0003 | 2026-04-11 | #49 | #47 | MERGE | yes — 0 missing cycle keys (claimed 0, −100%) | 134/134 | All claims hold; added max_text_only to _DEFAULT_CONFIG["cycle"], hoisted as module constant, +3 new test assertions |
| R-0004 | 2026-04-11 | #51 | #50 | MERGE | yes — 0 .get("summary_max_chars") call sites (claimed 0, −100%) | 136/136 | All claims hold; replaced stale .get() fallback (1500) with direct [] access, +2 new regression tests pinning key presence and value 3000 |
| R-0005 | 2026-04-11 | #53 | #52 | MERGE | yes — A=0 B=0 combined=0 (claimed 5→0, −100%) | 138/138 | All claims hold; replaced 4× _config.get("summary", {}) + 1× stale base_url fallback with direct [] access, +2 regression tests |
| R-0006 | 2026-04-11 | #55 | #54 | MERGE | yes — 0 stale defaults (claimed 0, −100%) | 140/140 | All claims hold; replaced 6 stale literal defaults in run_agent_single with _DEFAULT_CONFIG lookups, +2 regression tests |
| R-0007 | 2026-04-11 | #57 | #56 | MERGE | yes — 0 → 2 top-level user config keys loadable (+100%) | 144/144 | All claims hold; fixed _load_config() to copy top-level scalar keys (log_dir, log_prefix) from config.json, +4 regression tests |
| R-0008 | 2026-04-12 | #59 | #58 | MERGE | yes — 4 guards measured vs claimed 4 (+300%) | 147/147 | All claims hold; added _BLOCKED_FILENAMES check to _write, _append, _delete in tools/file.py, +3 regression tests |
| R-0009 | 2026-04-12 | #61 | #60 | MERGE | yes — A=1 (theme.SKY in on_session_start), B=5 (│ in tui.py), combined=6 (target ≥5) | 150/150 | All claims hold; redesigned welcome banner (SKY title, ─ bars, dimmed log paths) and TUI footer separators (│), +3 regression tests |
