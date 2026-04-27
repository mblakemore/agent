import pytest
import logging
import os
import agent
from unittest.mock import patch, MagicMock

log = logging.getLogger(__name__)

def test_validate_tool_call_pr_body_oserror():
    # Targets lines 332-333: except OSError: pass
    cmd = 'gh pr create --body "$(cat /tmp/pr-body-99999.md)"'
    blocked, msg = agent._validate_tool_call("exec_command", {"command": cmd}, False, log, is_cicd_builder=True)
    # OSError caught and passed — guard falls through to check Closes #N in _precmd itself
    # Since there's no Closes #N in the command string, it should be blocked.
    assert blocked

def test_salvage_tool_args_exception():
    # Targets lines 623-624: except Exception as e: log.debug(...)
    # Pass non-string raw_args to trigger exception in cleaned = raw_args.replace(...)
    result = agent._salvage_tool_args("exec_command", None, log)
    assert result is None

def test_setup_logger_default_history_dir():
    # Targets line 1169: history_dir = _HISTORY_DIR (the ELSE branch)
    orig = agent._config.pop("log_dir", None)
    try:
        logger = agent._setup_logger()
        assert logger is not None
    finally:
        if orig is not None:
            agent._config["log_dir"] = orig

def test_validate_tool_call_reads_pr_body_success(tmp_path):
    # Targets lines 330-331 (happy path)
    import shutil
    issue_num = "324"
    fname = str(tmp_path / f"pr-body-{issue_num}.md")
    with open(fname, "w") as f:
        f.write("Summary\nCloses #324\n")
    
    # Move to /tmp/ because agent.py looks there
    tmp_dest = f"/tmp/pr-body-{issue_num}.md"
    shutil.copy(fname, tmp_dest)
    
    try:
        cmd = f'gh pr create --body "$(cat {tmp_dest})"'
        blocked, msg = agent._validate_tool_call("exec_command", {"command": cmd}, False, log, is_cicd_builder=True)
        assert not blocked
    finally:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)

def test_build_summary_prompt_with_cicd_globals():
    # Targets lines 837-843 (in _build_summary_prompt)
    orig = (agent._cicd_worktree_path, agent._cicd_edited_files, agent._cicd_issue_number, agent._cicd_pr_number, agent._cicd_branch)
    try:
        agent._cicd_worktree_path = "/tmp/worktree"
        agent._cicd_edited_files = {"agent.py"}
        agent._cicd_issue_number = 324
        agent._cicd_pr_number = 325
        agent._cicd_branch = "cicd/324-test"
        prompt = agent._build_summary_prompt("old summary", [])
        assert "Worktree path" in prompt
        assert "Issue: #324" in prompt
    finally:
        (agent._cicd_worktree_path, agent._cicd_edited_files, agent._cicd_issue_number,
         agent._cicd_pr_number, agent._cicd_branch) = orig

