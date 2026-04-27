import os
import pytest
from unittest.mock import patch, MagicMock
from tools.exec_command import fn

def test_exec_command_cicd_merge_no_issue():
    # Mock environment for CICD mode
    with patch.dict(os.environ, {"CICD_MODE": "1"}):
        # Mock subprocess.run to simulate 'gh pr view' returning a body without 'Closes #N'
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout="This is a PR body without a linked issue.")
            
            result = fn(command="gh pr merge 123")
            assert "BLOCKED: PR #123 body does not contain 'Closes #N'" in result

def test_exec_command_cicd_merge_invalid_issue():
    # Mock environment for CICD mode
    with patch.dict(os.environ, {"CICD_MODE": "1"}):
        # Mock subprocess.run for two calls:
        # 1. gh pr view -> returns body with Closes #456
        # 2. gh issue view -> fails (returncode != 0)
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="This PR Closes #456"),
                MagicMock(returncode=1, stdout="", stderr="Issue not found")
            ]
            
            result = fn(command="gh pr merge 123")
            assert "BLOCKED: PR #123 references issue #456 but that issue does not exist" in result

def test_exec_command_cicd_merge_valid_issue():
    # Mock environment for CICD mode
    with patch.dict(os.environ, {"CICD_MODE": "1"}):
        # Mock subprocess.run for two calls:
        # 1. gh pr view -> returns body with Closes #456
        # 2. gh issue view -> succeeds (returncode 0)
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="This PR Closes #456"),
                MagicMock(returncode=0, stdout='{"number": 456, "state": "open"}')
            ]

            # Since it passes the guards, it should actually attempt to run the command.
            result = fn(command="gh pr merge 123")
            assert "BLOCKED" not in result

def test_exec_command_cicd_python3_merge_literal_not_blocked():
    # python3 -c "...gh pr merge 99..." should bypass the CICD_MODE guard
    # (cycle 96: python3 commands skip the guard entirely)
    with patch.dict(os.environ, {"CICD_MODE": "1"}):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            cmd = "python3 -c \"print('gh pr merge 99 --squash')\""
            result = fn(command=cmd)
            # Guard must not fire: the first two subprocess calls are for guard checks.
            # If guard fires, mock_run would be called for gh pr view. If not, it's skipped.
            assert "BLOCKED: PR #99" not in str(result)

def test_exec_command_cicd_cd_python3_merge_literal_not_blocked():
    # cd /path && python3 -c "...gh pr merge 99..." must not trigger the guard
    # (cycle 98: anchored regex — gh pr merge inside python3 string is not a top-level command)
    with patch.dict(os.environ, {"CICD_MODE": "1"}):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            cmd = "cd /repo && python3 -c \"blocked = ('gh pr merge 99 --squash' in 'x'); print(blocked)\""
            result = fn(command=cmd)
            assert "BLOCKED: PR #99" not in str(result)
