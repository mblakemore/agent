import os
import pytest
from tools.exec_command import fn

def test_cd_guard_symlink_allowed(monkeypatch, tmp_path):
    # Setup: Create a repo structure and a symlink to the root
    # /tmp/mnt/droid/repos/agent
    # /tmp/droid -> /tmp/mnt/droid
    
    mnt_droid = tmp_path / "mnt" / "droid"
    mnt_droid.mkdir(parents=True)
    repo_root = mnt_droid / "repos"
    repo_root.mkdir()
    agent_dir = repo_root / "agent"
    agent_dir.mkdir()
    
    # Create the symlink: /tmp/droid -> /tmp/mnt/droid
    droid_symlink = tmp_path / "droid"
    os.symlink(str(mnt_droid), str(droid_symlink))
    
    # Mock os.getcwd to be inside the agent dir
    monkeypatch.setattr(os, "getcwd", lambda: str(agent_dir))
    
    # The target path uses the symlink form
    # /tmp/droid/repos/agent/tests (relative to the symlinked root)
    target = str(droid_symlink / "repos" / "agent")
    command = f"cd {target} && pwd"
    
    result = fn(command=command)
    assert "Error: You are trying to cd to" not in result
    assert "exit=0" in result

def test_cd_guard_out_of_tree_blocked(monkeypatch, tmp_path):
    mnt_droid = tmp_path / "mnt" / "droid"
    mnt_droid.mkdir(parents=True)
    repo_root = mnt_droid / "repos"
    repo_root.mkdir()
    agent_dir = repo_root / "agent"
    agent_dir.mkdir()
    
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    
    monkeypatch.setattr(os, "getcwd", lambda: str(agent_dir))
    
    target = str(outside_dir)
    command = f"cd {target} && pwd"
    
    result = fn(command=command)
    assert "Error: You are trying to cd to" in result

def test_cd_guard_prefix_false_positive_blocked(monkeypatch, tmp_path):
    # Test that /foo doesn't allow cd to /foo-bar
    mnt_droid = tmp_path / "mnt" / "droid"
    mnt_droid.mkdir(parents=True)
    repo_root = mnt_droid / "repos"
    repo_root.mkdir()
    agent_dir = repo_root / "agent"
    agent_dir.mkdir()
    
    # Create a sibling directory that starts with the same string but isn't a child
    # Create a sibling directory that starts with the same string but isn't a child
    sibling_dir = repo_root.parent / "repos-extra"
    sibling_dir.mkdir()
    
    monkeypatch.setattr(os, "getcwd", lambda: str(agent_dir))
    
    target = str(sibling_dir)
    command = f"cd {target} && pwd"
    
    result = fn(command=command)
    # This should be blocked because /mnt/droid/repos-extra is not under /mnt/droid/repos
    assert "Error: You are trying to cd to" in result
