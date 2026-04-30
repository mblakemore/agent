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
    with patch('pathlib.Path.cwd') as mock_cwd:
        mock_cwd.return_value.resolve.return_value = Path("/home/user/repo")
        with patch('pathlib.Path.resolve') as mock_resolve:
            # The order in agent.py: 
            # 1. Path(file_path).resolve()
            # 2. Path(worktree_path).resolve()
            # 3. Path.cwd().resolve() (Wait, Path.cwd().resolve() is called separately)
            
            # Let's be careful with the side_effect order:
            # line 108: _abs_file = str(Path(file_path).resolve())
            # line 109: _abs_wt = str(Path(worktree_path).resolve())
            # line 110: _abs_cwd = str(Path.cwd().resolve())
            
            # But I patched Path.cwd already.
            # If I patch Path.resolve, it hits all Path(...).resolve() calls.
            
            # Let's use a side_effect that returns different things based on the path
            def resolve_side_effect(path_obj):
                p = str(path_obj)
                if "main_file.py" in p: return Path("/home/user/repo/main_file.py")
                if "wt" in p: return Path("/home/user/repo/wt")
                if "cwd" in p: return Path("/home/user/repo")
                return path_obj

            with patch('pathlib.Path.resolve', side_effect=resolve_side_effect):
                res, path = _check_worktree_guard("/home/user/repo/wt", "/home/user/repo/main_file.py")
                assert res is True
                assert path == "/home/user/repo/wt/main_file.py"

def test_exception():
    with patch('pathlib.Path.resolve', side_effect=Exception("Fail")):
        assert _check_worktree_guard("/tmp/wt", "/tmp/file") == (False, None)
