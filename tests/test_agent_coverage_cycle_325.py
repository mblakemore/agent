import logging
import os
import tempfile
from unittest.mock import patch, MagicMock
import agent

log = logging.getLogger(__name__)

def test_validate_tool_call_reads_pr_body_success():
    # Target Lines 327-333
    # The regex explicitly looks for files matching /tmp/pr-body(?:-\d+)?\.md
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        # We MUST name the file correctly to match the regex in agent.py
        # /tmp/pr-body-324.md is a match for /tmp/pr-body(?:-\d+)?\.md
        fname = "/tmp/pr-body-324.md"
        with open(fname, 'w') as f_real:
            f_real.write("Closes #324\n")
    try:
        cmd = f'gh pr create --body "$(cat {fname})"'
        blocked, msg = agent._validate_tool_call("exec_command", {"command": cmd}, False, log)
        assert not blocked, f"Tool call was blocked: {msg}"
    finally:
        if os.path.exists("/tmp/pr-body-324.md"):
            os.unlink("/tmp/pr-body-324.md")

def test_salvage_tool_args_garbled():
    # Target Lines 623-624
    raw = 'command": "ls -la'
    result = agent._salvage_tool_args("exec_command", raw, log)
    assert result is not None
    assert "command" in result
    assert result["command"] == "ls -la"

def test_build_summary_prompt_with_cicd_globals():
    # Target Lines 837-843
    orig = (agent._cicd_worktree_path, agent._cicd_edited_files, agent._cicd_issue_number, agent._cicd_pr_number, agent._cicd_branch)
    try:
        agent._cicd_worktree_path = "/tmp/worktree"
        agent._cicd_edited_files = {"agent.py"}
        agent._cicd_issue_number = 324
        agent._cicd_pr_number = 325
        agent._cicd_branch = "cicd/324-test"
        
        prompt = agent._build_summary_prompt("old summary", [])
        assert "Worktree path: /tmp/worktree" in prompt
        assert "Issue: #324" in prompt
        assert "PR: #325" in prompt
    finally:
        (agent._cicd_worktree_path, agent._cicd_edited_files, agent._cicd_issue_number,
         agent._cicd_pr_number, agent._cicd_branch) = orig

def test_setup_logger_log_dir_override(tmp_path):
    # Target Line 1169
    log_dir = str(tmp_path / "logs")
    
    old_log_dir = agent._config.get("log_dir")
    agent._config["log_dir"] = log_dir
    try:
        logger = agent._setup_logger()
        assert logger is not None
    finally:
        if old_log_dir is None:
            agent._config.pop("log_dir", None)
        else:
            agent._config["log_dir"] = old_log_dir
