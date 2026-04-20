import os
from pathlib import Path
from unittest.mock import patch
import pytest
import agent

@pytest.fixture
def fs_setup(tmp_path):
    """
    Sets up a directory structure:
    /tmp_path (CWD)
    ├── worktree/
    │   └── inside_wt.txt
    └── outside_wt.txt
    """
    cwd = tmp_path
    worktree = cwd / "worktree"
    worktree.mkdir()
    
    inside_wt = worktree / "inside_wt.txt"
    inside_wt.write_text("content")
    
    outside_wt = cwd / "outside_wt.txt"
    outside_wt.write_text("content")
    
    # A path completely outside the CWD
    external_path = tmp_path.parent / "external.txt"
    external_path.write_text("content")
    
    return {
        "cwd": cwd,
        "worktree": str(worktree),
        "inside_wt": str(inside_wt),
        "outside_wt": str(outside_wt),
        "external": str(external_path)
    }

def test_worktree_guard_none_paths(fs_setup):
    # 1. worktree_path is None
    res, path = agent._check_worktree_guard("some_file", None)
    assert res is False
    assert path is None

    # 2. file_path is None
    res, path = agent._check_worktree_guard(None, fs_setup["worktree"])
    assert res is False
    assert path is None

def test_worktree_guard_logic(fs_setup):
    # We mock Path.cwd to return our tmp_path
    with patch("agent.Path.cwd", return_value=fs_setup["cwd"]):
        
        # 3. file_path is outside the cwd
        # Should return (False, None)
        res, path = agent._check_worktree_guard(fs_setup["external"], fs_setup["worktree"])
        assert res is False
        assert path is None

        # 4. file_path is inside the worktree_path
        # Should return (False, None) because it's already "safe"
        res, path = agent._check_worktree_guard(fs_setup["inside_wt"], fs_setup["worktree"])
        assert res is False
        assert path is None

        # 5. file_path is inside the cwd but NOT inside the worktree_path
        # Should return (True, correction_path)
        res, path = agent._check_worktree_guard(fs_setup["outside_wt"], fs_setup["worktree"])
        assert res is True
        # The correction should be: worktree_path + relative_path_from_cwd
        expected_correction = os.path.join(fs_setup["worktree"], "outside_wt.txt")
        assert path == expected_correction

def test_worktree_guard_exception_handling(fs_setup):
    # Test that the try-except block handles invalid paths gracefully
    with patch("agent.Path.cwd", return_value=fs_setup["cwd"]):
        # Passing something that isn't a path-like object to trigger an exception
        res, path = agent._check_worktree_guard(12345, fs_setup["worktree"])
        assert res is False
        assert path is None
