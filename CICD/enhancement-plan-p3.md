# CICD Pipeline Enhancement Plan — Phase 3

Based on tests 10, 11, and 12 against `mblakemore/agent`. Phase 1 and 2 fixes are in the original `enhancement-plan.md`.

---

## Status

| Test | Builder turns | Reviewer turns | Outcome |
|------|--------------|----------------|---------|
| 10   | 40           | 25             | Merged PR #102 (pymupdf fix). Reviewer tried `git add reviews.md` outside repo — failed. |
| 11   | 42           | 24             | Merged PR #104 (prompt-toolkit). All template fixes from test 10 verified. |
| 12   | 45           | 38             | Merged PR #105 (search_files .agent dir). Builder forgot to file issue. Reviewer merged despite no linked issue. |

### Template fixes applied between tests 10 and 11
- `reviews.md` is local only — removed git add/commit from reviewer template
- Hard Rule 13 updated to state reviews.md lives outside repo

### Template fixes applied between tests 11 and 12
- Removed `|| true` from `gh pr ready` in reviewer merge flow
- Added `--json` to all `gh pr view` commands (GraphQL deprecation workaround)
- Strengthened builder worktree path warning in pinned section

### Verified working in test 12
- `gh pr ready <N> && gh pr merge <N> --squash --delete-branch` — clean merge
- `gh pr view <N> --json` — no GraphQL deprecation error
- Reviewer worktree placed in `worktrees/pr-105` (correct path)
- `reviews.md` written locally, no git operations attempted

---

## 1. Summary amnesia causes builder to skip phases

**Problem:** The async summarizer aggressively compresses context, losing track of which CICD phases have been completed. In test 12, the builder completed PERCEIVE and PROBE (turns 1-22) but after summary compression the context said "Task #1 (PERCEIVE) is complete. NEXT: Execute task #2: DECIDE." The builder then looped through investigation again, and eventually jumped straight from PROBE to IMPLEMENT without filing an issue or writing a plan.

**Evidence:**
- Test 12 builder: PR #105 body says `Closes #ISSUE` (literal placeholder, no real issue filed)
- Test 12 builder: No improvement plan written to `CICD/improvements/`
- Test 11 builder: `progress.md` header only (builder didn't fill in row after summary lost context)
- Test 10 reviewer: After summary, said "task #1 is underway" when all 5 tasks were complete

**Impact:** Critical — the builder produces PRs that violate the pipeline's own rules (no issue, no plan, no metric). The reviewer should catch these but doesn't always (see item #2).

**Fix (programmatic — recommended):** Add phase tracking to `agent.py` that persists across summary compression:
1. Detect phase transitions by monitoring tool calls (e.g., `gh issue create` = PLAN→IMPLEMENT transition, `git push` = IMPLEMENT→VERIFY)
2. Inject a phase-state reminder into every prompt: `"Phase checkpoint: PERCEIVE ✓, PROBE ✓, DECIDE ✗, PLAN ✗, IMPLEMENT ✗"` 
3. This state must NOT be part of the summarizable context — it should be in pinned/system content.

**Fix (template — complementary):** Add to builder pinned instructions:
```
PHASE GATES — you MUST complete these in order, do NOT skip:
- DECIDE: File a GitHub issue before any code changes
- PLAN: Write improvement plan to CICD/improvements/ before creating worktree
- IMPLEMENT: Create worktree, edit code, commit
- PR body MUST reference the real issue number, never use placeholder text
```

**Effort:** Large (programmatic), Small (template)

---

## 2. Reviewer does not enforce "no issue → CLOSE" rule

**Problem:** The reviewer's decision matrix says "CICD PR with no plan/metric/issue → CLOSE (hard-rule violation)." In test 12, PR #105 had `Closes #ISSUE` (literal placeholder) and no linked issue existed, yet the reviewer gave a MERGE verdict.

**Evidence:**
- Test 12: PR #105 title `"CICD search-hidden-fix: Allow searching in .agent directory (#ISSUE)"` — `#ISSUE` is not a valid reference
- Test 12: `gh issue list --state open` returned empty — no issue existed for this PR
- Reviewer wrote "Tests passed, metric verified" in reviews.md despite never successfully verifying the metric (search returned 0 matches 5+ times)

**Impact:** High — the reviewer is the last line of defense. If it doesn't enforce the rules, invalid PRs get merged to main.

**Fix (programmatic — recommended):** Add a pre-merge validation step in `agent.py` or as a reviewer-specific guard:
1. Before any `gh pr merge` command, extract the PR body and check for `Closes #\d+` (not `Closes #ISSUE` or other placeholders)
2. Verify the referenced issue actually exists: `gh issue view <N> --json state`
3. If validation fails, inject an error: "BLOCKED: PR body references #ISSUE which is not a valid issue number. Per decision matrix, CICD PRs without a linked issue must be CLOSE'd."

**Fix (template):** Add to reviewer pinned instructions:
```
PRE-MERGE CHECK (mandatory before any gh pr merge):
- Extract issue number from PR body "Closes #N"
- Run: gh issue view <N> --json state
- If issue doesn't exist or "Closes #N" is missing/placeholder → verdict is CLOSE
```

**Effort:** Medium (programmatic), Small (template)

---

## 3. Builder creates worktree in wrong location (persistent)

**Problem:** Despite strengthening the worktree path warning in pinned instructions (before test 12), the builder still created its worktree at `/tmp/cicd-test-12/temp/.../search-fix` inside the session directory instead of the designated `worktrees/` subdirectory. This has occurred in tests 6, 11, and 12.

**Evidence:**
- Test 12: `git worktree list` showed worktree at `.../search-fix` not `.../worktrees/search-fix`
- Test 11: Builder created worktree at `repo/103-missing-prompt-toolkit` (inside the repo clone itself)
- Test 6: Original observation documented in enhancement-plan.md item #1

**Impact:** Medium — causes branch deletion failures during reviewer cleanup, and in test 11 caused pytest collection errors when the reviewer's worktree picked up the builder's stray directory.

**Fix (programmatic — recommended):** Template instructions alone don't work for this. Add a guard in `exec_command` tool:
```python
if "git worktree add" in command:
    # Extract target path from command
    target = extract_worktree_path(command)
    if WORKTREE_ROOT and not target.startswith(WORKTREE_ROOT):
        return f"ERROR: Worktree must be created under {WORKTREE_ROOT}, not {target}. Use: git worktree add {WORKTREE_ROOT}/your-branch-name -b cicd/your-branch-name"
```
This requires passing `WORKTREE_ROOT` as an environment variable from `cicd.sh` to `agent.py`.

**Effort:** Medium

---

## 4. `gh pr ready || true` pattern (FIXED in test 12)

**Problem:** The reviewer template had `gh pr ready <N> 2>/dev/null || true` chained with `&& gh pr merge`. The `|| true` swallowed the ready command's exit code, so merge ran before the PR was actually marked ready, failing with "Pull Request is still a draft."

**Evidence:**
- Test 10: Reviewer hit this, recovered by retrying without `|| true`
- Test 11: Same issue, same recovery pattern

**Fix applied:** Removed `|| true`, changed to `gh pr ready <N>` then `gh pr merge <N> --squash --delete-branch`. Added explicit warning in pinned section: "NEVER chain with `|| true`".

**Status:** FIXED — verified in test 12, merge succeeded on first attempt.

---

## 5. `gh pr view` GraphQL deprecation error (FIXED in test 12)

**Problem:** Bare `gh pr view <N>` fails with `GraphQL: Projects (classic) is being deprecated`. The reviewer wastes 2-4 turns retrying before discovering `--json` workaround.

**Evidence:**
- Test 10: Reviewer hit error, recovered after retries
- Test 11: Reviewer wasted 4 turns retrying before switching to `--json`

**Fix applied:** Added `--json` to all `gh pr view` examples in reviewer template. Added explicit note: "Never use bare `gh pr view <N>` — it will fail."

**Status:** FIXED — verified in test 12, reviewer used `--json` without hitting the error.

---

## 6. `reviews.md` git commit outside repo (FIXED in test 11)

**Problem:** Reviewer tried to `git add /tmp/.../CICD/reviews.md` which is outside the repo clone, causing a git error.

**Evidence:**
- Test 10: `git add` failed with "is outside repository"

**Fix applied:** Updated Phase 7 TRACK, Hard Rule 13, and pinned section to explicitly state reviews.md is local only and must never be git-committed.

**Status:** FIXED — verified in tests 11 and 12, reviewer used direct file write.

---

## 7. Reviewer loops on failed metric verification

**Problem:** When the reviewer can't verify a metric, it retries the same command repeatedly instead of changing approach or flagging the issue. In test 12, it ran `search_files(pattern='search-test')` five consecutive times, all returning 0 matches, then merged anyway claiming "metric verified."

**Evidence:**
- Test 12: Turns 22-27, same `python3 -c "from tools.search_files import fn; print(fn(pattern='search-test', ...))"` command repeated 5 times
- Reviewer wrote "Tests passed, metric verified" despite never getting a successful metric measurement

**Impact:** Medium — undermines the reviewer's verification mandate. The whole point of independent verification is to catch claims that don't hold.

**Fix (template):** Add to reviewer pinned instructions:
```
METRIC VERIFICATION RULES:
- If the measurement command returns no useful result after 2 attempts, verdict is REQUEST_CHANGES
- Never claim "metric verified" if your measurement didn't produce a comparable number
- If the PR has no measurable metric (no before/after numbers), note "N/A" but check other criteria more strictly
```

**Fix (programmatic):** Detect 3+ identical consecutive tool calls and inject: "You have run the same command 3 times with the same result. Either change your approach or record the metric as unverifiable."

**Effort:** Small (template), Medium (programmatic)

---

## 8. Reviewer overwrites reviews.md instead of appending

**Problem:** The reviewer wrote reviews.md with `cat > reviews.md` (overwrite) using its own header format instead of appending to the existing table format. In test 12, it created a new "# CICD Review Log" header with different column names instead of the standard `| ReviewID | Date | PR | Issue | Verdict | Metric? | Tests | Reason |` format.

**Evidence:**
- Test 12 reviews.md: `| PR | Status | Verdict | Notes |` (wrong format)
- Test 11 reviews.md: `| R-0001 | 2026-04-13 | #104 | #103 | MERGE | N/A | PASS | ...` (correct format)

**Impact:** Low — tracking data is inconsistent, making it harder to parse across cycles.

**Fix (template):** The reviewer template already specifies the correct format. The issue is that after summary compression, the reviewer loses the format specification. Add to pinned instructions:
```
TRACK FORMAT (exact): echo "| R-NNN | YYYY-MM-DD | #PR | #ISSUE | VERDICT | metric_result | PASS/FAIL | reason |" >> <CICD_STATE>/reviews.md
Never overwrite reviews.md — always append with >>
```

**Effort:** Small

---

## 9. Builder creates dubious improvements when codebase is clean

**Problem:** When all obvious issues are already fixed, the builder burns 20+ turns searching for something to improve, then produces a marginal or questionable change. In test 12, it hardcoded `.agent` as an exception to hidden-directory filtering in `search_files.py` — a change with no clear use case and poor generalizability.

**Evidence:**
- Test 12: Builder spent turns 8-28 scanning files, checking TODOs, running dead code analysis, all returning nothing actionable
- Final change was a 1-line special-case that doesn't address a real user problem

**Impact:** Medium — wastes compute on low-value changes, and merged changes may need to be reverted.

**Fix (template):** Add a "no good target" exit path to the builder:
```
NULL RESULT CRITERIA — file a null result if:
- After 20 turns of PROBE, no issue with a measurable metric has been identified
- The best candidate is a style/preference change, not a bug or measurable improvement
- The change would require special-casing or hardcoding
A null result is a valid outcome. Do not force a change just to have output.
```

**Fix (programmatic):** If PROBE phase exceeds 20 turns without an `gh issue create`, inject a nudge: "You've spent 20 turns investigating. Either file an issue now with a clear metric, or declare a null result."

**Effort:** Small (template), Medium (programmatic)

---

## 10. Duplicate tool-call loop detection doesn't catch semantic repeats

**Problem:** `agent.py` has a tool-loop detector (lines 1968-2003) that catches identical batches repeated 3+ times. But the reviewer in test 12 ran the same `search_files` call 5 times because minor argument variations (different `glob` param) made each batch signature unique, bypassing detection.

**Evidence:**
- Test 12 reviewer turns 22-27: `fn(pattern='search-test', path=..., glob='**/*')` then `fn(pattern='search-test', path=..., glob='*')` then back to `glob='**/*'` — alternating signatures defeated the consecutive-identical check.

**Impact:** Medium — the model burns turns and context on futile retries.

**Fix (programmatic):** Extend the loop detector to track per-tool-name result hashes, not just batch signatures. If the same tool returns the same result 3+ times (regardless of argument variations), inject: "The {tool} tool has returned the same result {N} times despite different arguments. The approach is not working — change strategy or accept the result."

**Files:** `agent.py` lines 1968-2003 (tool-loop detection block)

**Effort:** Small

---

## 11. Builder search-fix worktree created outside worktrees/ despite guard

**Problem:** The `exec_command` cd-guard (lines 158-176) only blocks `cd` to paths outside the repo tree. It doesn't intercept `git worktree add` targeting wrong paths. The builder in test 12 ran `git worktree add /tmp/.../search-fix` (session dir, not worktrees/) without any error.

**Note:** This is the implementation detail for item #3. Separated here because the root cause is a missing guard type in `exec_command.py`, not just a template issue.

**Files:** `tools/exec_command.py` lines 153-196 (Guards section)

**Effort:** Small — pattern match on `git worktree add`, extract path, check against `WORKTREE_ROOT` env var.

---

## 12. Post-merge branch delete fails on stray builder worktrees

**Problem:** `gh pr merge --delete-branch` tries to delete the local branch, but fails if the builder left a worktree checked out on that branch. The reviewer then has to discover and clean up the stray worktree before the branch can be deleted.

**Evidence:**
- Test 12: `failed to delete local branch cicd/search-hidden-fix: failed to run git: error: cannot delete branch used by worktree at '.../search-fix'`

**Impact:** Low — the remote branch is still deleted by GitHub, and the reviewer eventually cleaned up. But it wastes 2-3 turns.

**Fix:** This is a downstream symptom of item #3/#11. If the worktree guard works, no stray worktrees exist to block deletion. No separate fix needed.

**Status:** Will resolve with item #3/#11.

---

## Priority order

| # | Enhancement | Type | Effort | Impact | Priority |
|---|---|---|---|---|---|
| 1 | Summary amnesia / phase tracking | Programmatic + Template | Large | Critical | P1 |
| 2 | Reviewer enforce issue-link check | Programmatic + Template | Medium | High | P1 |
| 7 | Reviewer loops on failed metric | Template + Programmatic | Small-Medium | Medium | P1 |
| 3/11 | Builder worktree path guard | Programmatic | Small | Medium | P2 |
| 9 | Null-result path for clean codebase | Template + Programmatic | Small-Medium | Medium | P2 |
| 10 | Semantic tool-loop detection | Programmatic | Small | Medium | P2 |
| 8 | Reviews.md append format | Template | Small | Low | P3 |
| 12 | Post-merge branch delete | — | — | Low | Resolved by #3 |
| 4 | `gh pr ready \|\| true` | Template | Small | Medium | DONE |
| 5 | `gh pr view --json` | Template | Small | Medium | DONE |
| 6 | `reviews.md` git commit | Template | Small | Medium | DONE |

---

## Pre-development: implementation details

### Batch A — Template-only (no code changes, apply immediately)

These are pinned-instruction edits. Apply all, then run test 13 to validate.

#### A1. Builder phase gates (items 1 template, 9)

**File:** `CICD/agent.md` pinned section (line 198)

**Add after step 8:**
```
PHASE GATES — you MUST complete these in order. Do NOT skip any:
- PERCEIVE: git fetch, check issues, run tests on main
- PROBE: Examine code for improvement targets
- DECIDE: File a GitHub issue with `gh issue create`. You MUST have an issue number before proceeding.
- PLAN: Write improvement plan to <CICD_STATE>/improvements/NNN-slug.md
- IMPLEMENT: Create worktree at <WORKTREE_ROOT>, edit code, commit
- PR body MUST reference the real issue number (e.g. "Closes #42"), never placeholder text

NULL RESULT — file a null result if:
- After 20 turns of PROBE, no issue with a measurable metric has been identified
- The best candidate is a style/preference change with no measurable improvement
- The change would require special-casing or hardcoding
A null result is a valid, expected outcome. Do not force a change.
```

**Acceptance:** Builder in test 13 files an issue before creating a worktree. If no good target, declares null result instead of forcing a dubious change.

#### A2. Reviewer pre-merge check and metric rules (items 2 template, 7, 8)

**File:** `CICD/reviewer.md` pinned section (line 181)

**Replace current pinned block with:**
```
MANDATORY REVIEW WORKFLOW — every cycle MUST follow these steps:
1. WORKTREE: `git fetch origin pull/<N>/head:review/pr-<N>` then `git worktree add <WORKTREE_ROOT>/pr-<N> review/pr-<N>`
2. TEST: Run full test suite in the review worktree — all must pass
3. METRIC: Re-measure the claimed metric from the PR body
   - If measurement returns no useful result after 2 attempts → verdict is REQUEST_CHANGES
   - Never claim "metric verified" unless your measurement produced a comparable number
   - If PR has no measurable metric, note "N/A" but apply other criteria strictly
4. PRE-MERGE CHECK (mandatory before ANY gh pr merge):
   - Extract issue number from PR body "Closes #N" — must be a real number, not placeholder
   - Run: `gh issue view <N> --json state` — issue must exist
   - If issue doesn't exist or "Closes #N" is missing/placeholder → verdict is CLOSE per decision matrix
5. VERDICT: Apply decision matrix — exactly one of MERGE/REQUEST_CHANGES/CLOSE/DEFER
6. ACT (merge): `gh pr ready <N>` then `gh pr merge <N> --squash --delete-branch`
   NEVER use --merge or --rebase. ALWAYS --squash --delete-branch.
   NEVER chain with `|| true` — it swallows errors and causes merge to fail on still-draft PRs.
7. TRACK: echo "| R-NNN | YYYY-MM-DD | #PR | #ISSUE | VERDICT | metric_result | PASS/FAIL | reason |" >> <CICD_STATE>/reviews.md
   Always APPEND with >>. Never overwrite. Do NOT git add or git commit reviews.md.
8. CLEANUP: `git worktree remove <WORKTREE_ROOT>/pr-<N> --force && git branch -D review/pr-<N>`

Never skip the worktree. Never skip independent verification. Never merge without testing.
```

**Acceptance:** Reviewer in test 13 checks for valid issue link before merging. If builder produced a PR with `Closes #ISSUE` (placeholder), reviewer CLOSE's it.

### Batch B — Programmatic (code changes to `agent.py`)

Apply after Batch A is validated in test 13. Test in test 14.

#### B1. Phase tracking injected into context (item 1 programmatic)

**File:** `agent.py`

**Design:** Track CICD phases by monitoring tool calls. Inject phase state into `_build_context_message()` so it survives summary compression.

**Detection heuristics** (monitor `exec_command` command strings):
| Signal | Phase transition |
|--------|-----------------|
| `gh issue list` or `gh issue search` | PERCEIVE active |
| `gh issue create` | DECIDE → PLAN (capture issue number from result) |
| `file` write to `improvements/` | PLAN complete |
| `git worktree add` | IMPLEMENT started |
| `git commit` | IMPLEMENT (code committed) |
| `git push` | IMPLEMENT → VERIFY |
| `gh pr create` | PR opened (capture PR number from result) |

**State storage:** New dict `_cicd_phase_state` alongside existing `_cycle_persisted`, `_has_committed`, etc. (around line 1560):
```python
_cicd_phase_state = {
    "perceive": False,
    "probe": False,  
    "decide": False,
    "plan": False,
    "implement": False,
    "verify": False,
    "track": False,
    "issue_number": None,
    "pr_number": None,
    "branch": None,
}
```

**Injection point:** `_build_context_message()` (line 830). Add after pinned instructions:
```python
if _cicd_phase_state and any(_cicd_phase_state.values()):
    phase_line = "PHASE CHECKPOINT: " + " | ".join(
        f"{k.upper()} {'✓' if v else '✗'}" 
        for k, v in _cicd_phase_state.items() 
        if k not in ("issue_number", "pr_number", "branch")
    )
    if _cicd_phase_state["issue_number"]:
        phase_line += f"\nIssue: #{_cicd_phase_state['issue_number']}"
    if _cicd_phase_state["pr_number"]:
        phase_line += f"  PR: #{_cicd_phase_state['pr_number']}"
    parts.append(phase_line)
```

**Gating logic** (in the exec_command result handler, after line 2147):
```python
# Block git push if no issue was filed (CICD phase gate)
if "git push" in _cmd and _cicd_phase_state.get("decide") is False:
    # Don't actually block, but inject a warning
    conversation_history.append({
        "role": "user", 
        "content": "[SYSTEM WARNING: You are pushing code but no GitHub issue was filed. "
                   "The reviewer will CLOSE this PR. File an issue first with gh issue create.]"
    })
```

**Activation:** Only when `--nudge` is enabled (CICD mode). Gated by checking if pinned instructions contain "CICD" or similar marker, or via a new `--cicd` flag.

**Risk:** False positives on non-CICD repos. Mitigate with `--cicd` flag or auto-detect from pinned instructions.

**Acceptance:** In test 14, after summary compression, the context message includes `PHASE CHECKPOINT: PERCEIVE ✓ | PROBE ✓ | DECIDE ✓ | ...` and the builder doesn't skip phases.

#### B2. Pre-merge issue validation guard (item 2 programmatic)

**File:** `tools/exec_command.py` or `agent.py` (exec_command result handler)

**Design:** When `gh pr merge` is detected in an exec_command, extract the PR number, check the PR body for `Closes #\d+`, and validate the issue exists. This is cleaner as a guard in `exec_command.py` than in `agent.py`.

**Implementation in `exec_command.py`** (add to Guards section, after line 196):
```python
# Pre-merge validation: ensure PR has a valid linked issue
merge_match = re.search(r'gh\s+pr\s+merge\s+(\d+)', command)
if merge_match:
    pr_num = merge_match.group(1)
    # Check PR body for valid Closes #N
    check_cmd = f"gh pr view {pr_num} --json body --jq '.body'"
    result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True, cwd=home_cwd)
    body = result.stdout.strip()
    closes_match = re.search(r'Closes\s+#(\d+)', body)
    if not closes_match:
        return (
            f"BLOCKED: PR #{pr_num} body does not contain 'Closes #N' with a valid issue number. "
            f"Found body: {body[:200]}. "
            f"Per decision matrix, CICD PRs without a linked issue must be CLOSE'd, not merged."
        )
    issue_num = closes_match.group(1)
    # Verify issue exists
    verify_cmd = f"gh issue view {issue_num} --json number,state"
    verify_result = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True, cwd=home_cwd)
    if verify_result.returncode != 0:
        return (
            f"BLOCKED: PR #{pr_num} references issue #{issue_num} but that issue does not exist. "
            f"Per decision matrix, CICD PRs without a valid linked issue must be CLOSE'd."
        )
```

**Risk:** Blocks non-CICD PRs that don't use `Closes #N`. Mitigate: only activate when `CICD_MODE` env var is set (set by `cicd.sh`).

**Acceptance:** In test 14, if builder produces a PR with `Closes #ISSUE`, the `gh pr merge` command returns a BLOCKED error and the reviewer switches to CLOSE.

#### B3. Worktree path guard (items 3/11)

**File:** `tools/exec_command.py` Guards section (after line 196)

**Implementation:**
```python
# Worktree path guard: ensure worktrees are created in WORKTREE_ROOT
worktree_match = re.search(r'git\s+worktree\s+add\s+(\S+)', command)
if worktree_match:
    wt_path = worktree_match.group(1)
    wt_root = os.environ.get("WORKTREE_ROOT")
    if wt_root and not wt_path.startswith(wt_root):
        return (
            f"ERROR: Worktree must be created under {wt_root}, not {wt_path}. "
            f"Use: git worktree add {wt_root}/<branch-slug> -b <branch-name>"
        )
```

**Prerequisite:** `cicd.sh` must export `WORKTREE_ROOT`:
```bash
export WORKTREE_ROOT="${SESSION_DIR}/worktrees"
```
(Currently it's a local variable on line 45 of `cicd.sh`. Change to `export`.)

**Acceptance:** In test 14, builder attempts `git worktree add /tmp/.../search-fix` → gets error pointing to correct `worktrees/` path.

#### B4. Semantic tool-result loop detection (item 10)

**File:** `agent.py` lines 2163-2173 (result tracking block)

**Design:** Extend existing `_recent_tool_errors` tracking to detect same-result (not just same-call) patterns:
```python
# Track per-tool result hashes (not just error results)
_result_hash = hashlib.md5(result_str[:200].encode()).hexdigest()[:8]
_tool_result_key = (func_name, _result_hash)
_recent_tool_results.append(_tool_result_key)
if len(_recent_tool_results) > 10:
    _recent_tool_results.pop(0)
# Count how many of the last 6 results for this tool returned the same hash
_same_result_count = sum(1 for k in _recent_tool_results[-6:] if k == _tool_result_key)
if _same_result_count >= 3:
    conversation_history.append({
        "role": "user",
        "content": (
            f"SYSTEM: The {func_name} tool has returned the same result {_same_result_count} "
            f"times despite different arguments. Your approach is not working. "
            f"Either try a completely different method, or accept the current state and move on."
        ),
    })
```

**Acceptance:** In test 14, reviewer doesn't loop 5+ times on the same failed metric verification.

### Batch C — Nice-to-have (apply after Batches A and B are stable)

#### C1. CICD mode flag for `agent.py` and `cicd.sh`

Add `--cicd` flag to `agent.py` that:
- Enables phase tracking (B1)
- Enables pre-merge validation guard (B2)
- Enables worktree path guard (B3)
- Sets `CICD_MODE=1` env var for tool guards

`cicd.sh` passes `--cicd` alongside `--nudge`.

This replaces the current approach of relying on `WORKTREE_ROOT` env var and heuristics to detect CICD context.

#### C2. Summary preservation of phase state

Extend `_build_summary_prompt()` to explicitly instruct the summarizer:
```
ALWAYS preserve the current CICD phase state (which phases are complete, issue number, PR number).
These are critical for continuation and must appear in the DONE section.
```

This is defense-in-depth — B1's pinned injection is the primary mechanism, but this helps if the summary is the only context available (e.g., after aggressive compression).

---

## Dependency graph

```
Batch A (template-only)
├── A1: builder phase gates ──────────────────┐
└── A2: reviewer pre-merge + metric rules ────┤
                                              ├─→ Test 13
Batch B (code changes)                        │
├── B1: phase tracking (agent.py) ────────────┤
├── B2: pre-merge guard (exec_command.py) ◄───┤  depends on A2 being validated
├── B3: worktree guard (exec_command.py) ──────┤  depends on cicd.sh export
└── B4: semantic loop detection (agent.py) ────┤
                                              ├─→ Test 14
Batch C (nice-to-have)                        │
├── C1: --cicd flag ◄─────────────────────────┘  wraps B1-B3 activation
└── C2: summary phase preservation                defense-in-depth for B1
                                              └─→ Test 15
```

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Phase detection heuristics misfire on non-CICD prompts | Medium | Low | Gate behind `--cicd` flag (Batch C) or auto-detect from pinned instructions |
| Pre-merge guard blocks legitimate non-CICD PRs | Medium | Medium | Only activate when `CICD_MODE` env var is set |
| Worktree guard regex doesn't handle all `git worktree add` argument orders | Low | Low | Test against `git worktree add <path> <branch>` and `git worktree add -b <branch> <path>` variants |
| Phase tracking adds tokens to every context message | Low | Low | ~100 tokens per injection — negligible vs 45K context budget |
| Template changes make pinned section too long for model to follow | Medium | Medium | Keep pinned under 1500 chars; current is ~1056 (builder), additions add ~600 |
| Summary amnesia persists despite phase injection | Low | High | B1 injects into `_build_context_message` which is read every turn, not summarized |

## Success criteria for test 13 (template-only)

1. **Builder files an issue** before creating a worktree (or declares null result)
2. **Builder writes an improvement plan** to `CICD/improvements/` before coding
3. **Reviewer checks issue link** before merging — if placeholder, verdict is CLOSE
4. **Reviewer doesn't loop** on failed metric verification (stops after 2 attempts)
5. **Reviews.md** uses correct append format with `>>`
6. No regressions on test 11/12 fixes (gh pr ready, gh pr view --json, reviews.md local)

## Success criteria for test 14 (programmatic)

All of test 13 criteria, plus:
1. **Phase checkpoint** visible in context after summary compression
2. **Pre-merge guard** blocks `gh pr merge` on PRs with missing/placeholder issue links
3. **Worktree guard** blocks `git worktree add` outside `WORKTREE_ROOT`
4. **Semantic loop detection** stops reviewer from running same tool 5+ times
5. Builder completes full 8-phase cycle without losing phase context
