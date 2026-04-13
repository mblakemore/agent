# CICD Pipeline Enhancement Plan

Based on 6 test runs of the generalized CICD pipeline against `mblakemore/agent`.

---

## Status: Run 6 achieved first successful end-to-end cycle

Builder created a worktree, committed, pushed, opened PR #100. Reviewer verified, approved, merged. Exit code 0. Total: 98 turns (55 builder + 43 reviewer).

---

## 1. Worktree path correction

**Problem:** Builder creates worktree inside the repo (`repo/cicd/fix-special-chars`) instead of the designated `worktrees/` directory. The pinned instructions include the correct path template but the model substitutes its own.

**Impact:** Minor — worktree still functions, but clutters the repo checkout and breaks the reviewer's cleanup step (`git worktree remove`).

**Fix:** Strengthen the pinned instruction to be an exact command, not a template. Replace the `<WORKTREE_ROOT>/NNN-slug` placeholder with a concrete reminder that the worktree root was provided in the session override. Alternatively, add a programmatic guard in `exec_command` that detects `git worktree add` targeting a path inside the clone dir and returns an error nudging the agent to use the worktrees directory.

**Effort:** Small

---

## 2. PR body missing `Closes #N` trailer

**Problem:** Issue #70 stayed open after merge because the PR body didn't include `Closes #70`. The builder's `gh pr create` command omitted the trailer even though the template and pinned instructions mention it.

**Impact:** Medium — issues accumulate as open, breaking the dedup and priority logic in future cycles.

**Fix:** Add `Closes #ISSUE` to the pinned instructions' PR step as a literal requirement. Example:
```
7. PR: gh pr create --draft ... --body "... Closes #ISSUE"
   The body MUST contain "Closes #N" to auto-close the issue on merge.
```

**Effort:** Small

---

## 3. Grace period too short for TRACK phase

**Problem:** Builder ran out of grace turns (5) before writing the results file and progress row. The push happened at turn 51, and the grace period expired at turn 55, but the builder was still doing TRACK work (writing state, commenting on issue, marking tasks done).

**Impact:** Medium — results and progress logs are incomplete, making it harder for future cycles to understand history.

**Fix:** Increase `_CYCLE_GRACE_TURNS` from 5 to 8. The TRACK phase needs ~5-6 tool calls (results file, progress row, issue comment, label removal, task completion x2), and each takes 1-2 turns including model generation time.

**Effort:** Small

---

## 4. Reviewer uses `--merge` instead of `--squash`

**Problem:** Reviewer ran `gh pr merge 100 --merge --auto` instead of `--squash --delete-branch` as specified in the template (Hard Rule #7).

**Impact:** Low — merge history is noisier but functionally equivalent.

**Fix:** Add the exact merge command to the reviewer's pinned instructions:
```
5. ACT (merge): gh pr ready <N> && gh pr merge <N> --squash --delete-branch
   NEVER use --merge or --rebase. ALWAYS --squash --delete-branch.
```

**Effort:** Small

---

## 5. Builder spends too many turns investigating before editing

**Problem:** Builder spent 26 turns (18-44) reading code and thinking before making a single edit. Multiple `think` calls and file reads of the same sections. The efficiency rules say "decide by turn 10" but there's no equivalent for "edit by turn N".

**Impact:** Medium — burns context window budget on investigation, leaving less room for the verify/debug loop and TRACK phase.

**Fix (template):** Add to pinned instructions:
```
Time budget: Create worktree by turn 15. First code edit by turn 25. If you haven't edited code by turn 25, commit what you have or file a null result.
```

**Fix (programmatic):** Track turn-of-first-edit in `agent.py`. If turn > 30 and no file write has happened in the worktree, inject a nudge: "You have spent 30 turns without making a code change. Make your edit now or declare a null result."

**Effort:** Medium

---

## 6. PR had indentation bug, reviewer merged anyway

**Problem:** The builder's diff introduced extra indentation on a `for` loop and its body. The reviewer approved and merged without catching it.

**Impact:** Medium — the merged code has a syntax issue that would cause runtime errors. The reviewer's verification step (run tests) should have caught this if the tests cover that code path.

**Fix:** The reviewer template says to run the test suite in the worktree, which should catch syntax/indentation errors. Strengthen the reviewer's pinned instructions:
```
2. TEST: Run full test suite in the review worktree. If ANY test fails, verdict is REQUEST_CHANGES.
   Do NOT approve if tests fail. Do NOT skip the test step.
```

Also add a compile-check step to the reviewer workflow before running tests.

**Effort:** Small

---

## 7. Dependency bootstrap doesn't cover repos without manifest files

**Problem:** The `cicd.sh` bootstrap skips Python repos that don't have `requirements.txt` or `pyproject.toml`, even though they may have tests that benefit from `pytest`.

**Impact:** Low — the agent falls back to `unittest discover` which works. But `pytest` provides better output and is the standard.

**Fix:** After the existing dependency checks, add a Python fallback:
```bash
elif ls *.py tests/*.py 2>/dev/null | head -1 | grep -q .; then
    python3 -m venv "${SESSION_DIR}/.venv" 2>/dev/null && {
        . "${SESSION_DIR}/.venv/bin/activate"
        pip install --quiet pytest 2>/dev/null || true
        echo "    Python venv (pytest only): ${SESSION_DIR}/.venv"
    } || true
fi
```

**Effort:** Small

---

## 8. Async summary loses specifics of what was changed

**Problem:** After context compression, the summary says "Begin work on Issue #70" or "execution phase targeting Issue #70" but doesn't preserve the specific code change made (e.g., "added `sys.stdout.reconfigure(encoding='utf-8')` to `agent.py:main()`"). In run 5, this caused the model to hallucinate a `get_status()` fix it never made.

**Impact:** High — the model loses track of its own work and may repeat or contradict itself.

**Fix:** The summary prompt should instruct the summarizer to always preserve: (a) exact file paths and line numbers modified, (b) the specific code change description, (c) git branch and commit hashes. Review `_build_summary_prompt` and add explicit instructions to preserve these details.

**Effort:** Medium

---

## Priority order

| # | Enhancement | Effort | Impact | Priority |
|---|---|---|---|---|
| 2 | PR body `Closes #N` | Small | Medium | P1 |
| 3 | Grace period 5 → 8 | Small | Medium | P1 |
| 4 | Reviewer `--squash` | Small | Low | P1 |
| 1 | Worktree path | Small | Minor | P2 |
| 5 | Edit time budget | Medium | Medium | P2 |
| 6 | Reviewer compile check | Small | Medium | P2 |
| 7 | Pytest fallback bootstrap | Small | Low | P3 |
| 8 | Summary preserves specifics | Medium | High | P2 |

P1 items are all small pinned-instruction or config changes. P2 items require template + code changes. P3 is nice-to-have.
