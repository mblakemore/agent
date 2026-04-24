import subprocess
from unittest.mock import patch
from agent import _git_short_sha

def test_git_short_sha_success():
    with patch('subprocess.check_output') as mock_output:
        # The agent.py uses text=True, so it returns a string, not bytes
        mock_output.return_value = 'abc1234\n'
        assert _git_short_sha() == 'abc1234'

def test_git_short_sha_failure():
    with patch('subprocess.check_output') as mock_output:
        mock_output.side_effect = subprocess.CalledProcessError(1, 'git')
        assert _git_short_sha() == ""

def test_git_short_sha_generic_exception():
    with patch('subprocess.check_output') as mock_output:
        mock_output.side_effect = Exception("Unexpected error")
        assert _git_short_sha() == ""
