import os
import re
import pytest
from tools.exec_command import fn as exec_command

def test_cd_guard_symlinks(tmp_path, monkeypatch):
    # Setup: /mnt/droid/repos/agent
    # Create a symlink /droid -> /mnt/droid
    # We use tmp_path to simulate the environment
    
    root_dir = tmp_path / "mnt" / "droid"
    root_dir.mkdir(parents=True)
    
    # Create the actual repo directory
    repo_dir = root_dir / "repos" / "agent"
    repo_dir.mkdir(parents=True)
    
    # Create a target directory inside the repo
    target_dir = repo_dir / "test_dir"
    target_dir.mkdir()
    
    # Create the symlink /droid -> /mnt/droid
    # Note: We can't create a root symlink /droid without sudo,
    # so we simulate it by adding the symlink to our tmp_path
    symlink_root = tmp_path / "droid"
    os.symlink(str(root_dir), str(symlink_root))
    
    # Set the current working directory to the repo_dir
    monkeypatch.chdir(str(repo_dir))
    
    # Case 1: CD to in-tree path via symlink form (SHOULD BE ALLOWED)
    # The symlink form is /tmp/.../droid/repos/agent/test_dir
    symlink_path = str(symlink_root / "repos" / "agent" / "test_dir")
    cmd_ok = f"cd {symlink_path} && pwd"
    result_ok = exec_command(cmd_ok)
    assert "Error" not in result_ok, f"Should allow cd to symlinked in-tree path: {result_ok}"
    
    # Case 2: CD to out-of-tree path via symlink (SHOULD BE BLOCKED)
    # Create something outside /mnt/droid
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    symlink_outside = str(outside_dir)
    cmd_fail = f"cd {symlink_outside} && pwd"
    result_fail = exec_command(cmd_fail)
    assert "Error" in result_fail, "Should block cd to out-of-tree path"

def test_cd_guard_prefix_collision(tmp_path, monkeypatch):
    # Case 3: CD to a path differing only by a trailing-component prefix (e.g. /foo-x vs /foo)
    # This tests the os.sep anchoring.

    root_dir = tmp_path / "repo"
    root_dir.mkdir()
    worktree_dir = root_dir / "worktree"
    worktree_dir.mkdir()

    fake_other = tmp_path / "repo-other"
    fake_other.mkdir()

    # Now: home_cwd = .../repo/worktree -> repo_root = .../repo
    monkeypatch.chdir(str(worktree_dir))

    cmd_fail = f"cd {str(fake_other)} && pwd"
    result_fail = exec_command(cmd_fail)
    assert "Error" in result_fail, "Should block cd to paths that are just prefix matches"
