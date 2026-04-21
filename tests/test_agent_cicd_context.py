import os
import pytest
import agent

def test_build_context_footnote_cicd_checkpoint():
    # Setup mock state for CICD
    # These are module-level variables in agent.py
    agent._cicd_phase_state = {"perceive": True, "probe": False, "decide": False}
    agent._cicd_issue_number = "123"
    agent._cicd_pr_number = "456"
    agent._cicd_branch = "cicd/test-branch"
    agent._cicd_worktree_path = "/tmp/worktree"
    agent._cicd_edited_files = {"file1.py", "file2.py"}
    
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
    agent._cicd_phase_state = {}
    agent._cicd_issue_number = None
    agent._cicd_pr_number = None
    agent._cicd_branch = None
    agent._cicd_worktree_path = None
    agent._cicd_edited_files = None
    
    result = agent._build_context_footnote("Summary", "Initial")
    assert "PHASE CHECKPOINT" not in result["content"]

def test_build_context_footnote_empty_phase_state():
    # Case where _cicd_phase_state exists but all values are False
    agent._cicd_phase_state = {"perceive": False, "probe": False}
    result = agent._build_context_footnote("Summary", "Initial")
    assert "PHASE CHECKPOINT" not in result["content"]
