import subprocess
import sys
import pytest
from unittest.mock import patch, MagicMock
import importlib
import tools.gh_wrapper

def test_gh_wrapper_success():
    """Test that a successful gh command (exit 0) remains successful."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, 
            stdout="Success output", 
            stderr=""
        )
        
        with patch('sys.argv', ['gh_wrapper.py', 'pr', 'view']):
            with patch('sys.exit') as mock_exit:
                importlib.reload(tools.gh_wrapper)
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(0)

def test_gh_wrapper_deprecation_warning():
    """Test that exit 1 with deprecation warning is treated as exit 0."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, 
            stdout="PR details here", 
            stderr=f"Warning: {tools.gh_wrapper.DEPRECATION_WARNING}\nSome other noise"
        )
        
        with patch('sys.argv', ['gh_wrapper.py', 'pr', 'view']):
            with patch('sys.exit') as mock_exit:
                importlib.reload(tools.gh_wrapper)
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(0)

def test_gh_wrapper_real_failure():
    """Test that exit 1 without deprecation warning remains exit 1."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, 
            stdout="", 
            stderr="Error: PR not found"
        )
        
        with patch('sys.argv', ['gh_wrapper.py', 'pr', 'view']):
            with patch('sys.exit') as mock_exit:
                importlib.reload(tools.gh_wrapper)
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(1)

def test_gh_wrapper_other_exit_code():
    """Test that non-1 exit codes are preserved."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=2, 
            stdout="", 
            stderr="Critical failure"
        )
        
        with patch('sys.argv', ['gh_wrapper.py', 'pr', 'view']):
            with patch('sys.exit') as mock_exit:
                importlib.reload(tools.gh_wrapper)
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(2)

def test_gh_wrapper_no_args():
    """Test that no arguments returns exit 1."""
    with patch('sys.argv', ['gh_wrapper.py']):
        with patch('sys.exit') as mock_exit:
                importlib.reload(tools.gh_wrapper)
                tools.gh_wrapper.main()
                mock_exit.assert_called_with(1)
