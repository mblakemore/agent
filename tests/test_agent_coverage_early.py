import pytest
from unittest.mock import MagicMock, patch
import os
from pathlib import Path
from agent import _check_worktree_guard, _detect_hallucinated_read, _handle_cicd_file_edit

def test_check_worktree_guard_violation():
    # Setup paths to simulate a violation
    cwd = Path.cwd()
    wt_path = str(cwd / "worktrees/my-worktree")
    file_path = str(cwd / "some_file.py")
    
    # Mock resolve to return the paths we want
    with patch("pathlib.Path.resolve") as mock_resolve:
        mock_resolve.side_effect = lambda: Path(mock_resolve.call_args[0][0] if mock_resolve.call_args else "")
        # This is tricky with Path.resolve(), let's just use real temporary directories
        pass

def test_check_worktree_guard_real_dirs(tmp_path):
    cwd = tmp_path
    # Change process CWD to tmp_path for the duration of this test
    old_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        wt_path = str(cwd / "worktree")
        os.makedirs(wt_path)
        
        # Case 1: Violation (writing to CWD instead of worktree)
        file_path = str(cwd / "test.txt")
        is_violation, correction = _check_worktree_guard(file_path, wt_path)
        assert is_violation is True
        assert correction == str(Path(wt_path) / "test.txt")
        
        # Case 2: No violation (writing inside worktree)
        file_path_ok = str(Path(wt_path) / "test.txt")
        is_violation, correction = _check_worktree_guard(file_path_ok, wt_path)
        assert is_violation is False
        assert correction is None
        
        # Case 3: No worktree path provided
        is_violation, correction = _check_worktree_guard(file_path, None)
        assert is_violation is False
    finally:
        os.chdir(old_cwd)

def test_detect_hallucinated_read():
    # Case 1: Empty content
    is_hallucinated, reason = _detect_hallucinated_read("")
    assert is_hallucinated is False
    
    # Case 2: Claiming to read a file
    content = "I have read the contents of agent.py and found a bug."
    with patch("tools.file._accessed_files", set()):
        is_hallucinated, reason = _detect_hallucinated_read(content)
        assert is_hallucinated is True
        assert "Agent claimed to read agent.py" in reason

    # Case 3: Future intent (should NOT be hallucination)
    content_future = "I will read agent.py to verify the fix."
    with patch("tools.file._accessed_files", set()):
        is_hallucinated, reason = _detect_hallucinated_read(content_future)
        assert is_hallucinated is False

def test_handle_cicd_file_edit():
    log = MagicMock()
    history = []
    edited_files = set()
    state = {"plan": False}
    
    # Setup paths for the guard
    # We'll mock _check_worktree_guard to avoid complex path setup
    with patch("agent._check_worktree_guard") as mock_guard:
        mock_guard.return_value = (False, None)
        
        # Test basic edit
        args = {"path": "agent.py"}
        has_edited, reviewer_persisted = _handle_cicd_file_edit(
            args, history, "/tmp/wt", state, edited_files, False, False, 1, log
        )
        assert has_edited is True
        assert "agent.py" in edited_files
        
        # Test plan detection
        args_plan = {"path": "CICD/improvements/511-cov.md"}
        _handle_cicd_file_edit(
            args_plan, history, "/tmp/wt", state, edited_files, True, False, 2, log
        )
        assert state["plan"] is True
        
        # Test review persistence
        args_review = {"path": "CICD/reviews.md"}
        _, reviewer_persisted = _handle_cicd_file_edit(
            args_review, history, "/tmp/wt", state, edited_files, True, False, 3, log
        )
        assert reviewer_persisted is True

def test_handle_cicd_file_edit_violation():
    log = MagicMock()
    history = []
    edited_files = set()
    state = {}
    
    # Mock guard to return a violation
    with patch("agent._check_worktree_guard") as mock_guard:
        mock_guard.return_value = (True, "/correct/path/file.py")
        
        args = {"path": "/wrong/path/file.py"}
        _handle_cicd_file_edit(
            args, history, "/tmp/wt", state, edited_files, True, False, 4, log
        )
        
        assert len(history) == 1
        assert "WRONG PATH!" in history[0]["content"]
        assert "/correct/path/file.py" in history[0]["content"]
