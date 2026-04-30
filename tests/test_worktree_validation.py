import pytest
import os
from pathlib import Path
from unittest.mock import patch
from agent import _check_worktree_guard as _validate_worktree_write

def test_validate_worktree_write_none_inputs():
    assert _validate_worktree_write(None, "some/path") == (False, None)
    assert _validate_worktree_write("some/wt", None) == (False, None)
    assert _validate_worktree_write(None, None) == (False, None)

def test_validate_worktree_write_logic():
    # The code:
    # _abs_file = str(Path(file_path).resolve())
    # _abs_wt = str(Path(worktree_path).resolve())
    # _abs_cwd = str(Path.cwd().resolve())
    # if (_abs_file.startswith(_abs_cwd) and not _abs_file.startswith(_abs_wt)):
    #     _rel = os.path.relpath(_abs_file, _abs_cwd)
    #     _correct = os.path.join(worktree_path, _rel)
    #     return True, _correct

    # To make this work with mocks, we must ensure the mock's resolve() 
    # returns a value that satisfies the .startswith() checks.
    with patch('agent.Path.resolve') as mock_resolve, \
         patch('agent.Path.cwd') as mock_cwd, \
         patch('os.path.relpath') as mock_rel, \
         patch('os.path.join') as mock_join:
        
        abs_cwd = "/home/user/repo"
        abs_wt = "/mnt/worktree/528"
        abs_file = "/home/user/repo/src/agent.py"
        
        # 1. Path(file_path).resolve()
        # 2. Path(worktree_path).resolve()
        # 3. Path.cwd().resolve()
        mock_resolve.side_effect = [
            Path(abs_file),
            Path(abs_wt),
            Path(abs_cwd)
        ]
        
        # Path.cwd() is called, then .resolve() is called on the result.
        mock_cwd.return_value = Path(abs_cwd)
        
        mock_rel.return_value = "src/agent.py"
        mock_join.return_value = "/mnt/worktree/528/src/agent.py"
        
        wt_arg = "/mnt/worktree/528"
        file_arg = "/home/user/repo/src/agent.py"
        
        res, correction = _validate_worktree_write(wt_arg, file_arg)
        
        assert res is True
        assert correction == "/mnt/worktree/528/src/agent.py"

def test_validate_worktree_write_no_violation_in_wt():
    with patch('agent.Path.resolve') as mock_resolve, \
         patch('agent.Path.cwd') as mock_cwd:
        
        abs_wt = "/mnt/worktree/528"
        abs_file = "/mnt/worktree/528/src/agent.py"
        abs_cwd = "/home/user/repo"
        
        mock_resolve.side_effect = [
            Path(abs_file),
            Path(abs_wt),
            Path(abs_cwd)
        ]
        mock_cwd.return_value = Path(abs_cwd)
        
        res, correction = _validate_worktree_write(abs_wt, abs_file)
        assert res is False
        assert correction is None

def test_validate_worktree_write_no_violation_elsewhere():
    with patch('agent.Path.resolve') as mock_resolve, \
         patch('agent.Path.cwd') as mock_cwd:
        
        abs_wt = "/mnt/worktree/528"
        abs_file = "/tmp/some_file.txt"
        abs_cwd = "/home/user/repo"
        
        mock_resolve.side_effect = [
            Path(abs_file),
            Path(abs_wt),
            Path(abs_cwd)
        ]
        mock_cwd.return_value = Path(abs_cwd)
        
        res, correction = _validate_worktree_write(abs_wt, abs_file)
        assert res is False
        assert correction is None

def test_validate_worktree_write_exception():
    with patch('agent.Path.resolve', side_effect=RuntimeError("Boom")):
        res, correction = _validate_worktree_write("/tmp/wt", "/tmp/file")
        assert res is False
        assert correction is None
