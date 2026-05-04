import pytest
import os
from pathlib import Path
from unittest.mock import patch
from agent import _check_worktree_guard

def test_none_inputs():
    assert _check_worktree_guard(None, "/tmp/wt") == (False, None)
    assert _check_worktree_guard("/tmp/file", None) == (False, None)

def test_empty_inputs():
    assert _check_worktree_guard("", "/tmp/wt") == (False, None)
    assert _check_worktree_guard("/tmp/file", "") == (False, None)

def test_outside_cwd():
    with patch('pathlib.Path.cwd') as mock_cwd:
        mock_cwd.return_value.resolve.return_value = Path("/home/user/repo")
        with patch('pathlib.Path.resolve') as mock_resolve:
            # Path(file_path).resolve(), Path(worktree_path).resolve()
            mock_resolve.side_effect = [
                Path("/tmp/ext"), 
                Path("/home/user/repo/wt")
            ]
            assert _check_worktree_guard("/home/user/repo/wt", "/tmp/ext") == (False, None)

def test_inside_worktree():
    with patch('pathlib.Path.cwd') as mock_cwd:
        mock_cwd.return_value.resolve.return_value = Path("/home/user/repo")
        with patch('pathlib.Path.resolve') as mock_resolve:
            mock_resolve.side_effect = [
                Path("/home/user/repo/wt/file.py"), 
                Path("/home/user/repo/wt")
            ]
            assert _check_worktree_guard("/home/user/repo/wt", "/home/user/repo/wt/file.py") == (False, None)

def test_correction_needed():
    # Signature: _check_worktree_guard(file_path, worktree_path)
    # MagicMock is not a descriptor so Path.resolve side_effect receives no `self` —
    # use an ordered list (same pattern as test_outside_cwd / test_inside_worktree).
    # Path.cwd().resolve() goes through the cwd mock, not the patched Path.resolve.
    with patch('pathlib.Path.cwd') as mock_cwd:
        mock_cwd.return_value.resolve.return_value = Path("/home/user/repo")
        with patch('pathlib.Path.resolve') as mock_resolve:
            mock_resolve.side_effect = [
                Path("/home/user/repo/main_file.py"),  # Path(file_path).resolve()
                Path("/home/user/repo/wt"),             # Path(worktree_path).resolve()
            ]
            res, path = _check_worktree_guard(
                "/home/user/repo/main_file.py",  # file_path
                "/home/user/repo/wt",            # worktree_path
            )
            assert res is True
            assert path == "/home/user/repo/wt/main_file.py"

def test_exception():
    with patch('pathlib.Path.resolve', side_effect=Exception("Fail")):
        assert _check_worktree_guard("/tmp/wt", "/tmp/file") == (False, None)
