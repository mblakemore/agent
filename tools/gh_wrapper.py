#!/usr/bin/env python3
"""Wrapper for `gh` that distinguishes benign Projects-classic deprecation warnings
from real failures, and verifies write operations actually landed.

Usage:
    python3 tools/gh_wrapper.py pr edit <N> --body "<CONTENT>"
    python3 tools/gh_wrapper.py pr view <N> --json body
    ...etc. — any gh subcommand may be forwarded.

Exit codes:
    0  command succeeded (or only failed because of deprecation warning AND
       the post-write verification confirmed the operation landed)
    1  real failure (verification disagreed, gh missing, other stderr error)
    N  any non-1 exit code from gh is forwarded unchanged
"""
import subprocess
import sys

DEPRECATION_WARNING = "Projects (classic) is being deprecated"


def _parse_pr_edit_body(args):
    """If args is `pr edit <N> ... --body <CONTENT> ...`, return (pr_num, body).
    Otherwise return (None, None). --body-file and other variants return (None, None).
    """
    if len(args) < 4 or args[0] != "pr" or args[1] != "edit":
        return None, None
    pr_num = args[2]
    if not pr_num.isdigit():
        return None, None
    if "--body" not in args:
        return None, None
    i = args.index("--body")
    if i + 1 >= len(args):
        return None, None
    return pr_num, args[i + 1]


def _verify_pr_body(pr_num, expected_body):
    """Re-read the PR body and compare it to expected_body.
    Returns True if the bodies match (trailing-newline tolerant), False otherwise.
    A deprecation warning during the verify read is treated as a benign success
    (since we already have body content in stdout to compare).
    """
    result = subprocess.run(
        ["gh", "pr", "view", pr_num, "--json", "body", "--jq", ".body"],
        capture_output=True,
        text=True,
    )
    deprecation_only = (
        result.returncode == 1
        and result.stderr
        and DEPRECATION_WARNING in result.stderr
    )
    if result.returncode != 0 and not deprecation_only:
        return False
    return result.stdout.rstrip("\n") == expected_body.rstrip("\n")


def run_gh(args):
    """Execute gh and return (returncode, stdout, stderr).

    Handling:
    - exit=0: forwarded unchanged.
    - exit=1 + DEPRECATION_WARNING in stderr:
        * For `pr edit <N> --body <CONTENT>`: re-read the body. If it matches,
          surface exit=0. If it does NOT match, surface exit=1 (real failure
          — the deprecation warning concealed a write that did not land).
        * For other subcommands (reads): the warning is benign; surface exit=0.
    - exit=1 without the deprecation warning: forwarded unchanged (real failure).
    - other exit codes: forwarded unchanged.
    """
    if not args:
        return 1, "", "Usage: gh_wrapper.py <gh-command> [args...]\n"

    cmd = ["gh"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        if (
            result.returncode == 1
            and result.stderr
            and DEPRECATION_WARNING in result.stderr
        ):
            pr_num, expected_body = _parse_pr_edit_body(args)
            if pr_num is not None:
                if _verify_pr_body(pr_num, expected_body):
                    return 0, result.stdout, result.stderr
                augmented_stderr = (
                    result.stderr.rstrip("\n")
                    + "\n[gh_wrapper] post-write verification FAILED: "
                    f"`gh pr view {pr_num} --json body` did not match the submitted body. "
                    "Treating exit=1 as a real failure.\n"
                )
                return 1, result.stdout, augmented_stderr
            return 0, result.stdout, result.stderr

        return result.returncode, result.stdout, result.stderr

    except FileNotFoundError:
        return 1, "", "Error: 'gh' CLI not found in PATH\n"
    except Exception as e:
        return 1, "", f"Unexpected error: {e}\n"


def main():
    args = sys.argv[1:]
    returncode, stdout, stderr = run_gh(args)

    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)

    sys.exit(returncode)


if __name__ == "__main__":
    main()
