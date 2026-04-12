# 0044 — open-encoding-agent

- **Issue**: #90
- **Slug**: open-encoding-agent
- **Date**: 2026-04-12
- **Metric**: `grep -c "encoding='utf-8'" tools/web_fetch.py tools/think.py agent.py` → 0 → 10
- **Done-when**: all 10 `open()` calls have explicit `encoding='utf-8'` (or `errors='replace'` for read-mode), tests green

## Problem

Cycle 0042 (#87) fixed `tools/file.py` by adding explicit `encoding='utf-8'` to all
`open()` calls. The same class of defect persists in three other files:

| File | Line | Mode | Risk |
|------|------|------|------|
| `tools/web_fetch.py` | 71 | write | Fetched web content (emoji, unicode) silently corrupted |
| `tools/think.py` | 34 | read | config.json could fail on non-UTF-8 locale |
| `agent.py` | 117 | read | config.json read |
| `agent.py` | 999 | write | checkpoint write |
| `agent.py` | 1010 | read | checkpoint read |
| `agent.py` | 1050 | read | current-state.json read |
| `agent.py` | 1080 | write | current-state.json write |
| `agent.py` | 1088 | read | focus.json read |
| `agent.py` | 1092 | write | focus.json write |
| `agent.py` | 1857 | read | current-state.json read |

## Fix

1. `tools/web_fetch.py:71`: `open(save_path, "w", encoding='utf-8')`
2. `tools/think.py:34`: `open(config_path, encoding='utf-8', errors='replace')`
3. `agent.py` all 8 calls: add `encoding='utf-8'` (writes) or `encoding='utf-8', errors='replace'` (reads)

## Tests

- Add `test_web_fetch_encoding.py`: verify `fn()` handles unicode content without UnicodeDecodeError
- Extend `test_no_open_without_encoding.py` (or similar) to cover all three files

## Risks

- All changes are purely additive (adding keyword args) — no behavioral change on UTF-8 systems
- Low risk of test breakage
