# 0027 — file-blocked-write-append-delete — results

- Issue: #58
- Branch: cicd/0027-file-blocked-write-append-delete
- PR: (pending)
- Commit range: 2cecc88
- Date: 2026-04-11

## Metric

- Baseline: `grep -c 'if p.name in _BLOCKED_FILENAMES' tools/file.py` = 1 (read only)
- After:    4 (read + write + append + delete)
- Delta:    +3 (+300%)

## Test suite

- Before: 144 passing
- After:  147 passing (+3 new in `TestBlockedFilenames`)

## Probe re-run

- Log: /tmp/agent-cicd/probes/0027-pcount-after.log
- Verdict: PASS (2 turns, 1 tool call, correct answer 2016 — identical to before baseline)

## What I actually changed

- `tools/file.py`: added `if p.name in _BLOCKED_FILENAMES: return Error...` as the first check in `_write`, `_append`, and `_delete` (3 guards, 6 lines total)
- `tools/file.py` `_append`: placed blocked-filename check before the JSON-append guard so the error correctly identifies the file as an internal runtime file, not just a JSON file
- `tests/test_file_tool.py`: added `TestBlockedFilenames` with 3 regression tests:
  - `test_write_blocked_filename_returns_error` — verifies write to `conversation_checkpoint.json` fails and does not create the file
  - `test_append_blocked_filename_returns_error` — verifies append fails with "internal runtime file" message (proving the new check fires before the JSON guard)
  - `test_delete_blocked_filename_returns_error` — verifies delete fails and file still exists

## What I learned

- `_BLOCKED_FILENAMES` was a half-guard: it closed the most likely attack vector (agent reading its own checkpoint) but left write, append, and delete unguarded. The write gap was most dangerous because the read-first gate only activates when the file already exists.
- The JSON-append guard (`if p.suffix.lower() == '.json'`) coincidentally protected `conversation_checkpoint.json` from `_append`, but only because of its extension. Other future blocked filenames without `.json` extension would not get that protection.
- Placing the blocked-filename check first in each function is the cleanest pattern: it's the authoritative guard and produces a consistent error message.
