import os
import pytest
import re
from tools.exec_command import fn as exec_command

def test_cd_guard_symlinks(tmp_path):
    # Setup a mock repo structure
    # tmp_path is our 'home_cwd'
    # repo_root is tmp_path.parent
    repo_root = tmp_path.parent
    home_cwd = tmp_path
    
    # Create some directories in the repo
    repo_dir = repo_root / "my_project"
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "file.txt").write_text("hello")

    # Create an outside directory in a unique location to avoid FileExistsError
    outside_dir = tmp_path.parent.parent / f"outside_{os.getpid()}"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "secret.txt").write_text("secret")

    # Create symlinks
    # 1. Symlink to an in-tree directory
    sym_in = repo_root / "sym_in"
    if sym_in.exists() or sym_in.is_symlink():
        sym_in.unlink()
    os.symlink(repo_dir, sym_in)

    # 2. Symlink to an out-of-tree directory
    sym_out = repo_root / "sym_out"
    if sym_out.exists() or sym_out.is_symlink():
        sym_out.unlink()
    os.symlink(outside_dir, sym_out)

    # We need to mock os.getcwd() because exec_command uses it to determine home_cwd
    # and the repo_root is derived from it.
    import unittest.mock as mock
    with mock.patch("os.getcwd", return_value=str(home_cwd)):
        # Case 1: In-tree symlink (Should be ALLOWED)
        # The target path is absolute and uses a symlink, but resolves in-tree.
        cmd_in = f"cd {sym_in} && pwd"
        result_in = exec_command(cmd_in, session_id=None, new_session=True)
        assert "Error" not in result_in, f"Should allow cd to in-tree symlink: {result_in}"

        # Case 2: Out-of-tree symlink (Should be BLOCKED)
        cmd_out = f"cd {sym_out} && pwd"
        result_out = exec_command(cmd_out, session_id=None, new_session=True)
        assert "Error" in result_out, f"Should block cd to out-of-tree symlink: {result_out}"
        assert "outside your repo tree" in result_out

def test_cd_guard_prefix_collision(tmp_path):
    # Setup
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    home_cwd = repo_root / "workdir"
    home_cwd.mkdir(exist_ok=True)
    
    # Create a directory that starts with the same prefix but is NOT in the repo
    collision_dir = tmp_path / "repo-secret"
    collision_dir.mkdir(exist_ok=True)

    import unittest.mock as mock
    with mock.patch("os.getcwd", return_value=str(home_cwd)):
        # Case 3: Prefix collision (Should be BLOCKED)
        cmd_coll = f"cd {collision_dir} && pwd"
        result_coll = exec_command(cmd_coll, session_id=None, new_session=True)
        assert "Error" in result_coll, f"Should block prefix collision: {result_coll}"
