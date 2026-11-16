import os
import pytest
import cicd_gates
import agent


@pytest.fixture(autouse=True)
def _fresh_cicd_state():
    """These tests assign the module's state directly; leave a new cycle's state behind."""
    yield
    cicd_gates.reset()

def test_build_context_footnote_cicd_checkpoint():
    # Setup mock state for CICD
    # These are module-level variables in agent.py
    cicd_gates.phase_state = {"perceive": True, "probe": False, "decide": False}
    cicd_gates.issue_number = "123"
    cicd_gates.pr_number = "456"
    cicd_gates.branch = "cicd/test-branch"
    cicd_gates.worktree_path = "/tmp/worktree"
    cicd_gates.edited_files = {"file1.py", "file2.py"}
    
    summary_text = "Some work done"
    initial_files = "Initial file content"
    
    result = agent._build_context_footnote(summary_text, initial_files)
    content = result["content"]
    
    assert "PHASE CHECKPOINT" in content
    assert "PERCEIVE ✓" in content
    assert "PROBE ✗" in content
    assert "Issue: #123" in content
    assert "PR: #456" in content
    assert "Branch: cicd/test-branch" in content
    assert "Worktree path: /tmp/worktree" in content
    assert "Files already edited" in content
    assert "file1.py" in content
    assert "file2.py" in content

def test_build_context_footnote_no_cicd():
    # Reset state
    cicd_gates.phase_state = {}
    cicd_gates.issue_number = None
    cicd_gates.pr_number = None
    cicd_gates.branch = None
    cicd_gates.worktree_path = None
    cicd_gates.edited_files = None
    
    result = agent._build_context_footnote("Summary", "Initial")
    assert "PHASE CHECKPOINT" not in result["content"]

def test_build_context_footnote_empty_phase_state():
    # Case where _cicd_phase_state exists but all values are False
    cicd_gates.phase_state = {"perceive": False, "probe": False}
    result = agent._build_context_footnote("Summary", "Initial")
    assert "PHASE CHECKPOINT" not in result["content"]
