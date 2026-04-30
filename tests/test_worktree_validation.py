import pytest
import os
from pathlib import Path
from unittest.mock import patch
from agent import _check_worktree_guard as _validate_worktree_write

def test_validate_worktree_write_none_inputs():
    assert _validate_worktree_write(None, "some/path") == (False, None)
    assert _validate_worktree_write("some/wt", None) == (False, None)
    assert _validate_worktree_write(None, None) == (False, None)

def test_validate_worktree_write_valid_path():
    # Path is NOT in CWD, so it's not a violation
    with patch('os.getcwd', return_value="/tmp/cwd"):
        with patch('pathlib.Path.resolve', side_effect=lambda self: self):
            # File is in /tmp/other, CWD is /tmp/cwd. Not a violation.
            assert _validate_worktree_write("/tmp/wt", "/tmp/other/file.txt") == (False, None)

def test_validate_worktree_write_violation():
    # File is in CWD but NOT in worktree. Violation!
    with patch('os.getcwd', return_value="/tmp/cwd"):
        # Path.resolve() returns the path as is for simplicity in this mock
        with patch('pathlib.Path.resolve', side_effect=lambda self: self):
            cwd = "/tmp/cwd"
            wt = "/tmp/wt"
            file_path = "/tmp/cwd/src/main.py"
            
            # Mocking Path.cwd() as well because the code uses Path.cwd().resolve()
            with patch('pathlib.Path.cwd', return_value=Path(cwd)):
                res, correction = _validate_worktree_write(wt, file_path)
                assert res is True
                assert correction == "/tmp/wt/src/main.py"

def test_validate_worktree_write_in_worktree():
    # File is in worktree. No violation.
    with patch('os.getcwd', return_value="/tmp/cwd"):
        with patch('pathlib.Path.resolve', side_effect=lambda self: self):
            cwd = "/tmp/cwd"
            wt = "/tmp/wt"
            file_path = "/tmp/wt/src/main.py"
            
            with patch('pathlib.Path.cwd', return_value=Path(cwd)):
                res, correction = _validate_worktree_write(wt, file_path)
                assert res is False
                assert correction is None

def test_validate_worktree_write_exception():
    # Force an exception during resolution
    with patch('pathlib.Path.resolve', side_effect=Exception("Boom")):
        assert _validate_worktree_write("/tmp/wt", "/tmp/file") == (False, None)
