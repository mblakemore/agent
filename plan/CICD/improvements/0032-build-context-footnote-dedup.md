# 0032 — build-context-footnote-dedup

**Issue**: #68 — bug: _build_context builds context footnote twice — condensed path drops TOOL RULE hint
**Branch**: cicd/0032-build-context-footnote-dedup (will be created in Phase 6)

## Goal

Extract a `_build_context_footnote(summary_text, initial_files)` helper so `_build_context` builds the context message in one place instead of two, restoring the missing `TOOL RULE` hint in the condensed-summary path.

## Motivation

`_build_context` (agent.py lines 810-843) contains two nearly-identical code blocks that each build the context footnote message:
1. **Normal path** (lines 810-821): includes initial_files, progress summary, IMPORTANT note with `TOOL RULE` hint.
2. **Condensed path** (lines 833-843): same structure but *drops* the `TOOL RULE` hint.

The dropped hint means agents whose summary was condensed lose the instruction to use `exec_command` + heredoc for JSON writes. They may revert to `file(action='write', ...)` which is a known failure mode (cycle 0027 guards against it at the file-tool level, but the guidance in context is still the first line of defence).

Issue: #68  
Probe log: /tmp/agent-cicd/probes/0032-probe-before.log

## Success metric

- **Baseline**: 2 `parts = []` blocks inside `_build_context` in `agent.py`
- **Target**: 1 `parts = []` block (or zero — the helper owns the list, the caller sees none)
- **Measurement method**: `python3 -c "import ast, sys; ..."`  
  Concrete command:
  ```bash
  python3 -c "
  import re
  with open('agent.py') as f:
      src = f.read()
  # Isolate _build_context body
  m = re.search(r'def _build_context\b.*?(?=\ndef |\Z)', src, re.DOTALL)
  body = m.group(0) if m else ''
  count = body.count('parts = []')
  print(count)
  "
  ```
  Must print `0` or `1` after the fix (currently prints `2`).

## Scope

- **In**: `agent.py` — extract helper `_build_context_footnote(summary_text, initial_files)`, call it in both places inside `_build_context`
- **In**: new `tests/test_build_context_footnote.py` — regression guards
- **Out**: no other files; no change to any other function's behaviour

## Implementation steps

1. Add `_build_context_footnote(summary_text, initial_files)` above `_build_context` in `agent.py`. It should:
   - Build `parts = []`
   - Conditionally append `initial_files` if truthy
   - Append the progress summary line
   - Append the full IMPORTANT+TOOL RULE footnote string
   - Return `{"role": "user", "content": "\n\n".join(parts)}`
2. Replace the normal-path `parts = []; ...; context_msg = {...}` block (lines 810-822) with a single call to `_build_context_footnote(summary_state["text"], initial_files)`.
3. Replace the condensed-path `parts = []; ...; context_msg = {...}` block (lines 833-843) with a single call to `_build_context_footnote(summary_state["text"], initial_files)`.
4. Verify the `context_tokens` are still computed correctly after each assignment.
5. Write `tests/test_build_context_footnote.py`:
   - `test_single_parts_block_in_build_context`: asserts `_build_context` body has 0 or 1 `parts = []` occurrences (verifies dedup)
   - `test_footnote_helper_contains_tool_rule`: calls `_build_context_footnote("summary", None)` and asserts `"TOOL RULE"` is in the returned content
   - `test_footnote_helper_includes_initial_files`: calls with `initial_files="<files>"` and asserts the content starts with or contains that value
   - `test_footnote_helper_includes_summary`: asserts the content contains `"Progress summary of work done so far"` + the summary text

## Test plan

- Existing tests that must stay green: all 153 (run with `python3 -m unittest discover tests`)
- New tests: `tests/test_build_context_footnote.py` (4 methods as above)
- No live-probe re-run needed (static-only change, behaviour of `_build_context` is unchanged except TOOL RULE is now included in condensed path too)

## Risks & mitigations

- **Risk**: The `context_tokens = _estimate_tokens(context_msg)` line appears after both blocks — the refactored code must still assign `context_tokens` correctly.
  **Mitigation**: The helper returns the dict; the caller assigns `context_msg = _build_context_footnote(...)` and immediately follows with `context_tokens = _estimate_tokens(context_msg)`. No behaviour change.
- **Risk**: The condensed path used a slightly different string (no TOOL RULE) — adding it increases the condensed context_msg token count slightly.
  **Mitigation**: This is the intended fix. The 3-line addition to the condensed hint is ~30 tokens — negligible against a typical summary size.
- **Risk**: The helper captures `os.getcwd()` at call time (f-string). The normal path also does this. No change — same behaviour.

## Rollback

`git revert <commit>` on the single commit. The parent checkout is never touched.

## Closes

Closes #68
