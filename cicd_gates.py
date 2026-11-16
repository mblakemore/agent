"""The CICD loop's gates and the phase state they guard.

A CICD session (a builder or a reviewer working an issue through a worktree, a PR and
a verdict) is watched from the tool calls: the loop learns the issue, the PR, the
branch, the worktree and the edited files from exec_command results, ticks the phases,
and this module blocks the calls that would break the loop (a builder merging, a PR
without `Closes #N`, a push to main, a PR with files that do not compile) and verifies
the gh mutations the model claims. The command helpers anchor every check at a command
position, outside heredoc bodies and quoted strings. agent.py reads and assigns the
state here (`_cicd.pr_number`), so there is one copy of it.
"""
import os
import re
from pathlib import Path


# ── Paths and commands ────────────────────────────────────────────────


def check_worktree_guard(file_path, worktree_path):
    """
    Checks if a file write target is inside the repo clone instead of the worktree.
    Returns (is_violation, correction_path)
    """
    if not worktree_path or not file_path:
        return False, None

    try:
        _abs_file = str(Path(file_path).resolve())
        _abs_wt = str(Path(worktree_path).resolve())
        _abs_cwd = str(Path.cwd().resolve())
        
        if (_abs_file.startswith(_abs_cwd) and not _abs_file.startswith(_abs_wt)):
            _rel = os.path.relpath(_abs_file, _abs_cwd)
            _correct = os.path.join(worktree_path, _rel)
            return True, _correct
    except Exception:
        pass
    return False, None


# WS8.1 (beewatcher runs 092+, 114): guard/tracker regexes must only see
# actual command positions. Naive substring checks (`"git worktree add" in
# _cmd`) match inside heredoc bodies and quoted string literals — run 114
# burned turns 18-77 in a worktree-guard loop seeded by exactly this, and a
# heredoc line containing `&& git push` would fake-persist the cycle.
# Shell-separator anchor set shared by every command guard. Includes `$(`
# and backtick so unquoted command substitutions can't evade detection.
SHELL_ANCHOR = r"(?:^|&&\s*|;\s*|\|\|?\s*|\n\s*|\$\(\s*|`\s*)"


HEREDOC_BODY_RE = re.compile(
    r"<<-?\s*([\"']?)(\w+)\1.*?(?:\n\2\b|\Z)", re.DOTALL
)


def strip_noncommand_text(cmd: str) -> str:
    """Return cmd with heredoc bodies and quoted-string contents removed,
    and backslash line-continuations collapsed (cycle 36), so command
    detection only fires on real command positions.

    NOTE: only use the result for *detection/anchoring*. Content checks
    (e.g. `Closes #N` inside a --body string) must keep reading the raw
    command — quoted bodies are semantics-bearing there.
    """
    if not cmd:
        return cmd
    out = re.sub(r"\\\n", " ", cmd)
    out = HEREDOC_BODY_RE.sub(" ", out)
    out = re.sub(r"'[^']*'", "''", out)
    out = re.sub(r'"(?:\\.|[^"\\])*"', '""', out)
    return out


def cmd_has(cmd: str, phrase: str) -> bool:
    """True if `phrase` (words separated by whitespace) occurs at a command
    position — start of command or after a shell separator — outside
    heredoc bodies and quoted strings."""
    if not cmd or not phrase:
        return False
    stripped = strip_noncommand_text(cmd)
    pat = SHELL_ANCHOR + r"\s+".join(re.escape(w) for w in phrase.split()) + r"\b"
    return re.search(pat, stripped) is not None


# WS8.3 (run 096): tools/paths that count as legitimate TRACK work after the
# post-persist grace budget is exhausted. Everything else at that point is
# the start of a second PERCEIVE and gets blocked with a redirect.
TRACK_SCOPE_TOOLS = {"end_cycle", "task_tracker", "think", "sleep"}


TRACK_SCOPE_PATH_RE = re.compile(
    r"improvements/|progress-\d|reviews-\d|\.results\.md|journal"
)


def is_track_scope_call(func_name, func_args):
    """True if this tool call is plausible TRACK-phase work (results file,
    progress/review rows, git/gh bookkeeping) rather than new-cycle work."""
    args = func_args if isinstance(func_args, dict) else {}
    if func_name in TRACK_SCOPE_TOOLS:
        return True
    if func_name in ("file", "write_file", "append_file", "edit_file",
                     "read_file", "check_file"):
        path = str(args.get("path", "") or args.get("file_path", ""))
        return bool(TRACK_SCOPE_PATH_RE.search(path))
    if func_name == "exec_command":
        cmd = strip_noncommand_text(str(args.get("command", "")))
        if re.search(SHELL_ANCHOR + r"(?:git|gh)\b", cmd):
            return True
        return bool(TRACK_SCOPE_PATH_RE.search(cmd))
    return False


def handle_file_edit(func_args, conversation_history, cicd_worktree_path, cicd_phase_state, cicd_edited_files, has_edited, has_reviewer_persisted, turn, log):
    """
    Tracks file edits for CICD state management and enforces worktree guards.
    Returns (updated_has_edited, updated_has_reviewer_persisted)
    """
    if not has_edited:
        log.info("First file edit detected at turn %d", turn)
    
    has_edited = True
    _file_path = func_args.get("path", "")
    if _file_path:
        cicd_edited_files.add(_file_path)

    # Worktree guard
    is_violation, correct = check_worktree_guard(_file_path, cicd_worktree_path)
    if is_violation:
        log.warning("CICD: file write targets repo clone (%s), not worktree (%s)", _file_path, correct)
        conversation_history.append({
            "role": "user",
            "content": (
                f"[SYSTEM: WRONG PATH! You wrote to {_file_path} which is "
                f"inside the repo clone. Your worktree is at "
                f"'{cicd_worktree_path}'. You MUST write to "
                f"'{correct}' instead. Re-do this edit targeting "
                f"the worktree path now.]"
            ),
        })

    if "improvements/" in _file_path and _file_path.endswith(".md"):
        cicd_phase_state["plan"] = True
        log.info("CICD phase: plan written to %s", _file_path)

    if _file_path.endswith("reviews.md"):
        if not has_reviewer_persisted:
            has_reviewer_persisted = True
            log.info("CICD phase: review persisted to %s", _file_path)
        log.info("Reviewer persistence detected (reviews.md write) — completion signals now allowed")
    
    return has_edited, has_reviewer_persisted


# ── The phase state ───────────────────────────────────────────────────
# One cycle's CICD facts, module-level so the context window, the summary prompt and
# the gates read the same values as the loop that learns them from tool results.
phase_state = {}
issue_number = None
pr_number = None
branch = None
edited_files = set()      # edited paths, to survive summary compression
worktree_path = None      # captured on a successful `git worktree add`

PHASES = ("perceive", "probe", "decide", "plan", "implement", "verify", "track")


def reset():
    """A new cycle: every phase unticked, no issue, PR, branch, worktree or edits."""
    global phase_state, issue_number, pr_number, branch, edited_files, worktree_path
    phase_state = {p: False for p in PHASES}
    issue_number = None
    pr_number = None
    branch = None
    edited_files = set()
    worktree_path = None


def facts():
    """(worktree_path, edited_files, issue_number, pr_number, branch), for the summary prompt."""
    return worktree_path, edited_files, issue_number, pr_number, branch


# ── The gates ─────────────────────────────────────────────────────────


def validate_tool_call(func_name, func_args, cicd_issue_view_called, log, is_cicd_builder=False, is_cicd_reviewer=False):
    """
    Returns (is_blocked, error_message).
    If is_blocked is True, the tool should not be executed.
    """
    # Cycle 84 (runs 190+191 reviewer fix-forward failure mode): block the
    # reviewer from editing production .py files inside its review worktree.
    # Cycle 75 already says reviewer commits may only modify `tests/**`, but
    # the rule was prose only — runs 190 and 191 each saw the reviewer spend
    # 30+ turns rewriting `tools/exec_command.py` in the `pr-N` worktree
    # instead of issuing a verdict. The work is throwaway (no commit path)
    # and the missing verdict leaves the queue stuck.
    #
    # Detection: file tool, write/insert/replace action, target is a non-test
    # `.py` file, AND the target path is inside a `/worktrees/pr-<N>/` review
    # worktree (vs a builder worktree which has different naming). The marker
    # `/worktrees/pr-` is what reviewer.md prescribes (`git worktree add
    # <ROOT>/pr-<N> review/pr-<N>`).
    if func_name == "file" and isinstance(func_args, dict):
        _action = func_args.get("action", "")
        _path = func_args.get("path", "")
        if (_action in ("write", "insert", "replace")
                and _path.endswith(".py")
                and "/tests/" not in _path
                and not _path.startswith("tests/")
                and re.search(r"/worktrees/pr-\d+/", _path)):
            log.warning(
                "CICD: reviewer file edit BLOCKED — production .py inside review worktree (cycle 84)"
            )
            return True, (
                "Error: CICD reviewer file edit BLOCKED — you are editing a "
                f"production Python file ({_path}) inside a review worktree. "
                "Your role is REVIEWER, not BUILDER. Per cycle 75, reviewer "
                "commits may only modify `tests/**`. If the PR has a real bug "
                "in production code, the verdict is REQUEST_CHANGES (or CLOSE "
                "for destruction-class signatures) — leave a `gh pr comment "
                "<N> --body \"Verdict: REQUEST_CHANGES ...\"` citing the "
                "exact file:line and expected fix, then move on. Do NOT fix "
                "the code yourself — the builder owns production changes."
            )

    if func_name != "exec_command":
        return False, None

    _precmd = func_args.get("command", "") if isinstance(func_args, dict) else ""

    # WS8.1: anchor detection runs on stripped text (heredoc bodies + quoted
    # literals removed) so keywords inside string content can't false-fire.
    # Content checks below (pr-body `Closes #N`) keep using raw _precmd.
    _precmd_anchor = strip_noncommand_text(_precmd)

    # Cycle 96 — skip shell-level guards for python3/python invocations.
    # Guard regexes match CICD keywords appearing as string literals inside
    # python -c scripts, producing false positives (run 207 reviewer, turns 30-62).
    # WS8.1 tightening: only skip when the WHOLE command is a single python
    # invocation. The old blanket skip meant `python3 -c '...' && gh pr merge`
    # bypassed every guard. With quoted bodies stripped, a trailing compound
    # command is visible and the guards still run.
    if (re.match(r'\s*python3?\s', _precmd)
            and not re.search(r'&&|;|\|\|?|\n', _precmd_anchor)):
        return False, None

    # WS8.2 (run 110): the builder must NEVER merge — merge is the reviewer's
    # verdict. PRE-MERGE CHECK below only verifies issue state, so a builder
    # that ran `gh issue view` could still self-merge its own draft PR (run
    # 110: builder merged in 20 turns with no reviewer; merged test hung).
    # NOTE: reviewer sessions also match the "CICD Improvement Loop" builder
    # heuristic (reviewer.md shares the header), so is_cicd_builder is True
    # for reviewers too — the reviewer flag must win here.
    if (is_cicd_builder and not is_cicd_reviewer
            and re.search(SHELL_ANCHOR + r"gh\s+pr\s+merge\b", _precmd_anchor)):
        log.warning("CICD: gh pr merge BLOCKED — builder role may never merge (WS8.2, run 110)")
        return True, (
            "Error: CICD `gh pr merge` BLOCKED — you are the BUILDER. Merging "
            "is the REVIEWER's verdict, never the builder's. Your cycle ends "
            "at: draft PR opened + `Closes #N` + results file + TRACK row. "
            "The reviewer session will re-run tests and decide MERGE / "
            "REQUEST_CHANGES / CLOSE. The merge was NOT executed."
        )

    # PRE-MERGE CHECK — gated on CICD sessions only (issue #455: don't fire
    # for non-CICD repos that merge PRs without linked issues).
    if ((is_cicd_builder or is_cicd_reviewer)
            and re.search(SHELL_ANCHOR + r"gh\s+pr\s+merge\b", _precmd_anchor)
            and not cicd_issue_view_called):
        log.warning("CICD: gh pr merge BLOCKED — PRE-MERGE CHECK required (cycle 24)")
        return True, (
            "Error: CICD PRE-MERGE CHECK required. Before `gh pr merge`, you "
            "MUST run `gh issue view <N> --json state,labels,title,createdAt` "
            "on the linked issue and verify: state is OPEN, labels include "
            "`cicd` + `in-progress`, the title matches the PR's stated scope. "
            "Run the gh issue view now as a SEPARATE command, then re-attempt "
            "the merge. The merge was NOT executed."
        )
    
    # PR CREATE CHECK
    _precmd_body_check = _precmd
    # cycle 60: read pr-body file so Closes #N is visible even with $(cat ...) expansion
    # cycle 71: also handle per-issue filenames like /tmp/pr-body-324.md
    _pb_match = re.search(r'\$\(cat (/tmp/pr-body(?:-\d+)?\.md)\)', _precmd)
    if _pb_match:
        try:
            with open(_pb_match.group(1)) as _pf:
                _precmd_body_check = _precmd_body_check + " " + _pf.read()
        except OSError:
            pass
            
    # cycle 86: only enforce `Closes #N` requirement in CICD builder sessions;
    # general agent use on other repos may legitimately create PRs without a
    # linked issue (roadmap commits, README updates, etc.).
    if (is_cicd_builder
            and re.search(SHELL_ANCHOR + r"gh\s+pr\s+create\b", _precmd_anchor)
            and not re.search(r'Closes\s+#\d+', _precmd_body_check, re.IGNORECASE)):
        log.warning("CICD: gh pr create blocked — body missing valid Closes #N (cycle 44)")
        return True, (
            "Error: CICD gh pr create blocked — the --body must contain "
            "`Closes #<N>` where N is a numeric issue number (e.g. `Closes #123`). "
            "Non-numeric references like `Closes #slug` or missing Closes trailer "
            "cause the reviewer to CLOSE this PR. "
            "File the issue first with `gh issue create --label in-progress --label cicd ...`, "
            "note the issue number, then include `Closes #<number>` in the PR body. "
            "The PR was NOT created."
        )

    # Cycle 81 (run 186 failure mode): block `git push origin main` pre-execute.
    # Cycle 37's post-execute WARNING let the push through — run 186 builder
    # committed `ed67439` directly to main, push succeeded with only a log line,
    # builder reverted as `42a1dac`, then hit hard-cap with no PR opened. 5
    # turns + the entire cycle were wasted. Make it a hard block at the same
    # point as the other CICD pre-checks.
    # cycle 81 — gated on CICD sessions only (issue #455: non-CICD repos may
    # legitimately push directly to main).
    if ((is_cicd_builder or is_cicd_reviewer)
            and re.search(SHELL_ANCHOR + r"git\s+push\b[^&;|]*\borigin\s+main\b", _precmd_anchor)):
        log.warning("CICD: git push origin main BLOCKED — must use feature branch (cycle 81)")
        return True, (
            "Error: CICD `git push origin main` BLOCKED — direct pushes to main "
            "are prohibited. All CICD work must land via a feature branch + PR.\n"
            "If you have a local commit on main that shouldn't be there:\n"
            "  1. `git reset --hard origin/main` (drops the local commit)\n"
            "  2. `git worktree add <WORKTREE_ROOT>/<slug> -b cicd/<slug>`\n"
            "  3. Re-apply your changes inside the worktree\n"
            "  4. `git push -u origin cicd/<slug>` then `gh pr create --draft ...`\n"
            "Do NOT use `git revert HEAD && git push origin main` — that still "
            "pushes to main. The push was NOT executed."
        )

    # Cycle 80 (run 183 failure mode): block `gh pr create` if any edited .py
    # file fails py_compile. Run 183 PR #398 shipped IndentationError at the
    # four "Session ended" sites + a double `continue` in `_iter_stream_chunks`.
    # Cycle 65's "git checkout HEAD -- agent.py and reapply" was prose only;
    # the builder ignored it and 93 turns + a R-0008 REQUEST_CHANGES were lost.
    # This guard makes py_compile a hard gate at the same point as the Closes
    # check — the only way past is to fix the syntax.
    if re.search(SHELL_ANCHOR + r"gh\s+pr\s+create\b", _precmd_anchor):
        import py_compile
        _syntax_errors = []
        for _path in sorted(edited_files):
            if not _path.endswith(".py"):
                continue
            try:
                py_compile.compile(_path, doraise=True)
            except py_compile.PyCompileError as _e:
                _syntax_errors.append(f"{_path}: {str(_e).strip()}")
            except OSError:
                pass
        if _syntax_errors:
            log.warning(
                "CICD: gh pr create BLOCKED — %d edited .py file(s) fail py_compile (cycle 80)",
                len(_syntax_errors),
            )
            return True, (
                "Error: CICD gh pr create BLOCKED — edited Python files fail py_compile:\n  - "
                + "\n  - ".join(_syntax_errors)
                + "\nFix the syntax errors and re-verify with `python3 -m py_compile <file>` "
                "before opening the PR. If the error keeps moving line-to-line, "
                "`git checkout HEAD -- <file>` and reapply the change with exact "
                "indentation (cycle 65). The PR was NOT created."
            )

    return False, None


def verify_gh_mutation(command: str, result: str, log) -> str:
    """Cycle 77 — verify claimed gh PR/issue mutations against GitHub's actual state.

    Runs 142 + 146 hallucinated exec_command tool results that *looked* like
    successful `gh pr create` / `gh pr merge` output (URLs, exit=0) but the
    referenced PR did not actually exist on GitHub. The model then proceeded
    as if the work was done.

    This helper runs after any `gh pr create|merge|close|ready` or
    `gh issue create|close` command. It extracts the PR/issue number the
    model claimed in the tool output, queries GitHub directly for its real
    state, and appends a SUPERVISION note to the tool result if the state
    does not match the mutation's expected outcome. The note becomes part
    of the tool-result the model sees next turn, so fabricated numbers are
    corrected with real data the model cannot override.

    Scoped to the CICD exec_command path; returns ``result`` unchanged for
    non-gh commands so non-CICD runs see zero overhead.
    """
    action_match = re.search(r"\bgh\s+(pr|issue)\s+(create|merge|close|ready)\b", command)
    if not action_match:
        return result
    kind_word, action = action_match.group(1), action_match.group(2)
    # Forensic probe (cycle 77 step 3): prove the hook fires on gh mutations.
    # Keep at INFO so it appears in the standard CICD run log.
    log.info("cicd.gh_verify.enter kind=%s action=%s cmd_preview=%r",
             kind_word, action, command[:80])
    expected_state = {
        "create": None,    # just needs to exist
        "merge": "MERGED",
        "close": "CLOSED",
        "ready": "OPEN",
    }[action]

    # Parse claimed number from the result: URLs like .../pull/123 or .../issues/456
    url_match = re.search(
        r"github\.com/[^\s]+/(pull|issues)/(\d+)", result, re.IGNORECASE)
    if not url_match:
        # Also accept bare "#N" patterns in case gh output form differs
        hash_match = re.search(r"(?:pull request|issue) #(\d+)", result, re.IGNORECASE)
        if not hash_match:
            return result
        path = "pr" if kind_word == "pr" else "issue"
        num = hash_match.group(1)
    else:
        path = "pr" if url_match.group(1).lower() == "pull" else "issue"
        num = url_match.group(2)

    try:
        import subprocess
        proc = subprocess.run(
            ["gh", path, "view", num, "--json", "state,title"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("cicd.gh_verify.probe_failed path=%s num=%s error=%s", path, num, e)
        return result

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip().splitlines()[-1][:160]
        log.warning("cicd.gh_verify.missing path=%s num=%s", path, num)
        return result + (
            f"\n\n[CICD SUPERVISION (cycle 77): gh {path} view {num} failed — "
            f"the {path} #{num} referenced in the tool output DOES NOT EXIST on GitHub. "
            f"Do not proceed as if the {action} succeeded. "
            f"gh stderr: {err}]"
        )

    try:
        import json as _json
        data = _json.loads(proc.stdout)
        actual_state = (data.get("state") or "").upper()
    except (ValueError, KeyError) as e:
        log.warning("cicd.gh_verify.parse_failed num=%s error=%s", num, e)
        return result

    if expected_state and actual_state != expected_state:
        log.warning(
            "cicd.gh_verify.state_mismatch path=%s num=%s expected=%s actual=%s",
            path, num, expected_state, actual_state,
        )
        return result + (
            f"\n\n[CICD SUPERVISION (cycle 77): gh {path} view {num} reports state={actual_state}, "
            f"but `gh {kind_word} {action}` claims {expected_state}. "
            f"The {action} did NOT take effect as narrated. Investigate before proceeding.]"
        )

    return result


def checkpoint_text():
    """The CICD phase checkpoint as one block of text, or "" when no phase is set.
    Changes whenever a phase flips, a PR/issue/branch is learned or a file is edited."""
    if not (phase_state and any(phase_state.values())):
        return ""
    phases = " | ".join(
        f"{k.upper()} {'✓' if v else '✗'}"
        for k, v in phase_state.items()
    )
    phase_line = f"PHASE CHECKPOINT: {phases}"
    if issue_number:
        phase_line += f"\nIssue: #{issue_number}"
    if pr_number:
        phase_line += f"  PR: #{pr_number}"
    if branch:
        phase_line += f"  Branch: {branch}"
    if worktree_path:
        phase_line += f"\nWorktree path: {worktree_path} (ALL file edits and commands MUST target this path, not the repo clone)"
    if edited_files:
        phase_line += f"\nFiles already edited (DO NOT re-verify — move to commit/push): {', '.join(sorted(edited_files))}"
    return phase_line
