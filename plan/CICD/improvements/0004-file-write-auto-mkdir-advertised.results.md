# 0004 — file-write-auto-mkdir-advertised — results

- Issue: #7
- Branch: `cicd/0004-file-write-auto-mkdir-advertised`
- PR: (opened in Phase 8)
- Date: 2026-04-11

## Metric

**Primary**: tool-call count on the P-impl probe — "Create word_freq.py + tests/test_word_freq.py + run unittest". Measured as:

```bash
grep -c '^INFO: TOOL CALL: ' <probe-log>
```

| | Before | After |
|---|---|---|
| Tool calls | 4 | **3** |
| Turns | 5 | 4 |
| Wall time | ~47s | ~35s |
| Tests created | 5/5 pass ✓ | 5/5 pass ✓ |
| `mkdir -p tests` wasted call | present | **gone** |

- Baseline log: `/tmp/agent-cicd/probes/0004-pimpl-before.log`
- After log:   `/tmp/agent-cicd/probes/0004-pimpl-after.log`
- Delta: **−1 tool call (−25%)**, **−1 turn (−20%)**, **−12s wall (−25%)**. Target was ≤ 3 tool calls and ≤ 4 turns. PASS.

The after-run's three tool calls (from the log) were, in order:
1. `file({"action": "write", "path": "word_freq.py", ...})`
2. `file({"action": "write", "path": "tests/test_word_freq.py", ...})` ← straight to the nested path, no pre-mkdir
3. `exec_command({"command": "python3 -m unittest tests/test_word_freq.py"})`

The baseline run burned an extra turn on `exec_command({"command": "mkdir -p tests"})` between steps 1 and 2. That call is gone now because the `write` action's description tells the model it's unnecessary (and forbids it).

## Test suite

- Before: 106 passing
- After:  108 passing (+2 new)

New tests in `tests/test_file_tool.py`:
- `test_description_advertises_auto_mkdir` — pins the `write` line of `file.definition["function"]["description"]` to contain both `"Parent directories are created automatically"` and `"do NOT call mkdir"`. Drift tripwire: trimming the description trips this test before the probe ever runs again.
- `test_write_creates_missing_parent_dirs` — tempdir + absolute path `<tmp>/a/b/hello.txt`; asserts the write succeeds and both `a/` and `a/b/` are created on disk. Guards the underlying behavior at `tools/file.py:165` so the promise the description now makes stays true under future refactors.

## Probe re-run

- Log: `/tmp/agent-cicd/probes/0004-pimpl-after.log`
- Session log: `/tmp/probe-0004-after/.agent/history/session_*.log`
- Verdict: **PASS** on the first after-run (no debug iterations needed).

## What I actually changed

- `tools/file.py` — one-line description update on the `write` action, extending the existing bullet with a second sentence: *"Parent directories are created automatically — do NOT call mkdir or exec_command before writing a file into a new directory."* No change to the write logic itself (already auto-mkdirs via `p.parent.mkdir(parents=True, exist_ok=True)`).
- `tests/test_file_tool.py` — new file, two tests (wording tripwire + behavior guard). This is the first test module in `tests/` that imports `tools.file` directly; it uses the same sys.path header as `tests/test_search_files.py`.

## What I learned

- **Directive phrasing beats factual phrasing when you're trying to override an LLM prior.** Cycle 0003 found the same shape: "the behavior is already right, the model just doesn't know it." I considered saying only "Parent directories are created automatically." — which is true and informative — but paired it with an explicit "do NOT call mkdir or exec_command before writing" because the model's prior is *strong* here (every Bash tutorial mkdirs before writing). The paired phrasing was enough on the first after-run; I didn't need the fallback of also mentioning it in the `IMPORTANT:` paragraph.
- **Test descriptions can be pinned on short semantic substrings without being fragile.** Asserting two ~30-char phrases survives any rewording that keeps the meaning; it only trips when someone actually removes the signal. This is cheaper than pinning the whole paragraph byte-for-byte.
- **Two gates catch two different drifts.** The wording tripwire catches description rot; the behavior test catches a refactor that drops `p.parent.mkdir`. Either drift alone would make the `write` description a lie — now either drift trips a test.
- **Single-probe wall time fell 25% without being the target.** The turn savings are the cause: one fewer round trip to the LLM endpoint. Worth noting that metric changes compound — the tool-call-count target is the cleanest thing to assert on, but the user-visible effect is faster task completion.
