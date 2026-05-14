import importlib
from unittest.mock import patch, MagicMock

import tools.gh_wrapper


def _reload():
    importlib.reload(tools.gh_wrapper)


def test_gh_wrapper_success():
    """A successful gh command (exit 0) remains successful."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Success output", stderr=""
        )
        with patch("sys.argv", ["gh_wrapper.py", "pr", "view"]):
            with patch("sys.exit") as mock_exit:
                _reload()
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(0)


def test_gh_wrapper_deprecation_warning_on_read():
    """exit=1 with deprecation warning on a non-write subcommand is treated as exit 0."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="PR details here",
            stderr=f"Warning: {tools.gh_wrapper.DEPRECATION_WARNING}\nSome other noise",
        )
        with patch("sys.argv", ["gh_wrapper.py", "pr", "view"]):
            with patch("sys.exit") as mock_exit:
                _reload()
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(0)


def test_gh_wrapper_real_failure():
    """exit=1 without deprecation warning remains exit 1."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error: PR not found"
        )
        with patch("sys.argv", ["gh_wrapper.py", "pr", "view"]):
            with patch("sys.exit") as mock_exit:
                _reload()
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(1)


def test_gh_wrapper_other_exit_code():
    """Non-1 exit codes are preserved."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="Critical failure"
        )
        with patch("sys.argv", ["gh_wrapper.py", "pr", "view"]):
            with patch("sys.exit") as mock_exit:
                _reload()
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(2)


def test_gh_wrapper_no_args():
    """No arguments returns exit 1."""
    with patch("sys.argv", ["gh_wrapper.py"]):
        with patch("sys.exit") as mock_exit:
            _reload()
            tools.gh_wrapper.main()
            mock_exit.assert_called_with(1)


def test_pr_edit_body_deprecation_with_verified_write():
    """exit=1 + deprecation warning on `pr edit --body` where body DID land -> exit 0."""
    edit_result = MagicMock(
        returncode=1,
        stdout="",
        stderr=f"GraphQL: {tools.gh_wrapper.DEPRECATION_WARNING} ... (projectCards)\n",
    )
    verify_result = MagicMock(returncode=0, stdout="new body contents\n", stderr="")

    with patch("subprocess.run", side_effect=[edit_result, verify_result]) as mock_run:
        rc, out, err = tools.gh_wrapper.run_gh(
            ["pr", "edit", "1234", "--body", "new body contents"]
        )

    assert rc == 0, (rc, err)
    # Two subprocess.run calls: the original edit + the verify read.
    assert mock_run.call_count == 2
    verify_cmd = mock_run.call_args_list[1][0][0]
    assert verify_cmd[:5] == ["gh", "pr", "view", "1234", "--json"]


def test_pr_edit_body_deprecation_with_unchanged_body():
    """exit=1 + deprecation warning on `pr edit --body` where body did NOT land -> exit 1.

    Regression for run 1007 failure mode: stderr matched the deprecation warning,
    but the write itself silently failed (body unchanged on the server). Without
    verification, the wrapper would have surfaced this as success and the agent
    would have proceeded with a stale PR body.
    """
    edit_result = MagicMock(
        returncode=1,
        stdout="",
        stderr=f"GraphQL: {tools.gh_wrapper.DEPRECATION_WARNING} ... (projectCards)\n",
    )
    # Verify reports a totally different body (old/stale content).
    verify_result = MagicMock(returncode=0, stdout="OLD STALE BODY\n", stderr="")

    with patch("subprocess.run", side_effect=[edit_result, verify_result]):
        rc, out, err = tools.gh_wrapper.run_gh(
            ["pr", "edit", "1234", "--body", "new body contents"]
        )

    assert rc == 1, (rc, err)
    assert "post-write verification FAILED" in err


def test_pr_edit_body_deprecation_verify_tolerates_deprecation_on_read():
    """Verify-read may itself trip the deprecation warning; if stdout matches, success."""
    edit_result = MagicMock(
        returncode=1,
        stdout="",
        stderr=f"{tools.gh_wrapper.DEPRECATION_WARNING}\n",
    )
    verify_result = MagicMock(
        returncode=1,
        stdout="matching body\n",
        stderr=f"{tools.gh_wrapper.DEPRECATION_WARNING}\n",
    )

    with patch("subprocess.run", side_effect=[edit_result, verify_result]):
        rc, _, _ = tools.gh_wrapper.run_gh(
            ["pr", "edit", "5", "--body", "matching body"]
        )

    assert rc == 0


def test_pr_edit_body_file_falls_through_to_lenient_path():
    """--body-file is not parsed for verify (we don't have the content in args).
    Without an explicit --body, the wrapper falls through to lenient deprecation
    handling (the existing behavior). This documents the current contract.
    """
    edit_result = MagicMock(
        returncode=1,
        stdout="",
        stderr=f"{tools.gh_wrapper.DEPRECATION_WARNING}\n",
    )

    with patch("subprocess.run", side_effect=[edit_result]) as mock_run:
        rc, _, _ = tools.gh_wrapper.run_gh(
            ["pr", "edit", "1234", "--body-file", "/tmp/body.md"]
        )

    assert rc == 0
    # Only one call — no verify because we couldn't parse the body.
    assert mock_run.call_count == 1


def test_pr_edit_non_numeric_pr_number_no_verify():
    """If the PR number isn't a digit string, skip verification (defensive)."""
    edit_result = MagicMock(
        returncode=1,
        stdout="",
        stderr=f"{tools.gh_wrapper.DEPRECATION_WARNING}\n",
    )
    with patch("subprocess.run", side_effect=[edit_result]) as mock_run:
        rc, _, _ = tools.gh_wrapper.run_gh(
            ["pr", "edit", "not-a-number", "--body", "x"]
        )
    assert rc == 0
    assert mock_run.call_count == 1


def test_parse_pr_edit_body_returns_none_when_body_has_no_value():
    # Lines 33-35 execute when `--body` is the last token in args (no value follows).
    # `_parse_pr_edit_body` must return (None, None) so the wrapper falls through
    # to the lenient deprecation path instead of raising IndexError.
    edit_result = MagicMock(
        returncode=1,
        stdout="",
        stderr=f"{tools.gh_wrapper.DEPRECATION_WARNING}\n",
    )
    with patch("subprocess.run", side_effect=[edit_result]) as mock_run:
        rc, _, _ = tools.gh_wrapper.run_gh(["pr", "edit", "1234", "--body"])
    # Fell through to lenient deprecation path: exit 0, no verify call.
    assert rc == 0
    assert mock_run.call_count == 1


def test_verify_returns_false_when_verify_read_fails_without_deprecation():
    # Line 56 executes when the verify `gh pr view` returns exit != 0 with stderr
    # that does NOT contain the deprecation warning — a real verify-read error.
    # End-to-end the wrapper must surface exit=1 with "post-write verification FAILED".
    edit_result = MagicMock(
        returncode=1,
        stdout="",
        stderr=f"GraphQL: {tools.gh_wrapper.DEPRECATION_WARNING} ... (projectCards)\n",
    )
    # Verify read fails with an unrelated error (not the deprecation warning).
    verify_result = MagicMock(
        returncode=1, stdout="", stderr="Error: Could not resolve to a PullRequest\n"
    )

    with patch("subprocess.run", side_effect=[edit_result, verify_result]):
        rc, _, err = tools.gh_wrapper.run_gh(
            ["pr", "edit", "1234", "--body", "new content"]
        )

    assert rc == 1, (rc, err)
    assert "post-write verification FAILED" in err


def test_run_gh_returns_error_when_gh_binary_missing():
    # Lines 100-101 execute when `subprocess.run` raises FileNotFoundError
    # (the `gh` CLI is not installed / not in $PATH).
    with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        rc, out, err = tools.gh_wrapper.run_gh(["pr", "view", "1"])

    assert rc == 1
    assert out == ""
    assert "'gh' CLI not found in PATH" in err


def test_run_gh_returns_error_on_unexpected_subprocess_exception():
    # Lines 102-103 execute when `subprocess.run` raises any exception other
    # than FileNotFoundError (e.g. PermissionError, OSError).
    with patch("subprocess.run", side_effect=PermissionError("denied")):
        rc, out, err = tools.gh_wrapper.run_gh(["pr", "view", "1"])

    assert rc == 1
    assert out == ""
    assert "Unexpected error:" in err
    assert "denied" in err
