# 0044 — open-encoding-agent — results

- Issue: #90
- Branch: cicd/0044-open-encoding-agent
- PR: (see below)
- Commit range: e829418
- Date: 2026-04-12

## Metric

- Baseline: `grep -c "encoding='utf-8'" tools/web_fetch.py tools/think.py agent.py` = 0 (all three files combined)
- After: 10 (web_fetch.py: 1, think.py: 1, agent.py: 8)
- Delta: +10 (+∞%)

## Test suite

- Before: 195 passing
- After: 205 passing (+10 new static regression tests)

## Probe

- P-count before: 8s wall time, 1 tool call (correct path)
- P-enum before: 38s wall time, 1 tool call, result truncated (56 matches found across 148 files)

## What changed

- `tools/web_fetch.py:71`: `open(save_path, "w")` → `open(save_path, "w", encoding="utf-8")`
- `tools/think.py:34`: `open(config_path)` → `open(config_path, encoding="utf-8", errors="replace")`
- `agent.py:117`: `open(config_path, "r")` → `open(config_path, "r", encoding="utf-8", errors="replace")`
- `agent.py:999`: `open(_CHECKPOINT_PATH, "w")` → `open(_CHECKPOINT_PATH, "w", encoding="utf-8")`
- `agent.py:1010`: `open(_CHECKPOINT_PATH)` → `open(_CHECKPOINT_PATH, encoding="utf-8", errors="replace")`
- `agent.py:1050`: `open(state_path)` → `open(state_path, encoding="utf-8", errors="replace")`
- `agent.py:1080`: `open(state_path, "w")` → `open(state_path, "w", encoding="utf-8")`
- `agent.py:1088`: `open(focus_path)` → `open(focus_path, encoding="utf-8", errors="replace")`
- `agent.py:1092`: `open(focus_path, "w")` → `open(focus_path, "w", encoding="utf-8")`
- `agent.py:1857`: `open(_state_path("current-state.json"))` → `open(..., encoding="utf-8", errors="replace")`

## New tests

- `tests/test_open_encoding_agent.py` — 10 static assertions (one per call site)

## Verdict

PASS — all 205 tests green, metric moved 0 → 10.
