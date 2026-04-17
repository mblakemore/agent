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
