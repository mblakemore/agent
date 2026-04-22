import pytest
from unittest.mock import MagicMock, patch
import agent
import logging

# Setup logger to avoid errors during tests
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("test")

def test_build_context_initial_files_coverage():
    """
    Targets lines 1117-1118:
    if initial_files:
        selected.insert(0, {"role": "user", "content": initial_files + "\n\nContinue."})
    """
    conversation_history = [
        {"role": "assistant", "content": "Msg 1"},
        {"role": "assistant", "content": "Msg 2"}
    ]
    summary_state = {"text": ""}
    initial_files = "File content here"
    ctx_size = 8192
    max_tokens = 4096
    
    agent._TOOLS_TOKENS = None  # reset so the mock below takes effect
    with patch('agent._estimate_tools_tokens', return_value=100), \
         patch('agent._estimate_tokens', return_value=10):

        selected, oldest_idx = agent._build_context(
            conversation_history, summary_state, initial_files, ctx_size, max_tokens, log
        )

        # Check if the synthetic user message was inserted
        assert len(selected) > 0
        assert selected[0]["role"] == "user"
        assert "File content here\n\nContinue." in selected[0]["content"]

def test_build_context_fallback_user_msg_coverage():
    """
    Targets lines 1119-1123:
    else:
        for i in range(oldest_idx - 1, -1, -1):
            if conversation_history[i]["role"] == "user":
                selected.insert(0, conversation_history[i])
                break
    """
    conversation_history = [
        {"role": "user", "content": "Initial request"},
        {"role": "assistant", "content": "Response 1"},
        {"role": "assistant", "content": "Response 2"}
    ]
    summary_state = {"text": ""}
    initial_files = None
    ctx_size = 8192
    max_tokens = 4096
    
    # max_messages_override=2 caps selection to [Response 1, Response 2] (no user);
    # fallback loop then inserts [Initial request] from earlier in history.
    agent._TOOLS_TOKENS = None  # reset so the mock below takes effect
    with patch('agent._estimate_tools_tokens', return_value=100), \
         patch('agent._estimate_tokens', side_effect=[10, 10, 10]):

        selected, oldest_idx = agent._build_context(
            conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
            max_messages_override=2
        )

        # [Initial request] prepended → 3 total
        assert len(selected) == 3
        assert selected[0]["role"] == "user"
        assert selected[0]["content"] == "Initial request"

def test_build_context_no_fallback_coverage():
    """
    Tests the case where no user message is found in history (lines 1120-1123)
    """
    conversation_history = [
        {"role": "assistant", "content": "Response 1"},
        {"role": "assistant", "content": "Response 2"}
    ]
    summary_state = {"text": ""}
    initial_files = None
    ctx_size = 8192
    max_tokens = 4096
    
    agent._TOOLS_TOKENS = None  # reset so the mock below takes effect
    with patch('agent._estimate_tools_tokens', return_value=100), \
         patch('agent._estimate_tokens', return_value=10):

        selected, oldest_idx = agent._build_context(
            conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
            max_messages_override=1
        )

        # No user message to fallback to, selected should just contain the assistant msg
        assert len(selected) == 1
        assert selected[0]["role"] == "assistant"

def test_validate_tool_call_reads_pr_body_file(tmp_path):
    # Targets lines 329-330
    # We need to create the file in /tmp/ because agent.py looks there specifically
    issue_num = "324"
    filename = f"/tmp/pr-body-{issue_num}.md"
    content = "Summary\nCloses #324\n"
    with open(filename, "w") as f:
        f.write(content)
    try:
        log = logging.getLogger("test")
        blocked, msg = agent._validate_tool_call(
            "exec_command",
            {"command": f'gh pr create --body "$(cat {filename})"'},
            False, log
        )
        # Should not be blocked because the file exists and contains Closes #324
        assert blocked is False
    finally:
        import os
        if os.path.exists(filename):
            os.remove(filename)

def test_salvage_tool_args_exec_command():
    # Targets lines 620-621
    log = logging.getLogger("test")
    # Provide a string that is not valid JSON but contains a key:value pair
    result = agent._salvage_tool_args("exec_command", 'command: "ls -la"', log)
    assert result == {"command": "ls -la"}

def test_build_summary_prompt_cicd_facts():
    # Targets lines 834, 838, 840
    old_wt = agent._cicd_worktree_path
    old_issue = agent._cicd_issue_number
    old_pr = agent._cicd_pr_number
    
    agent._cicd_worktree_path = "/some/worktree"
    agent._cicd_issue_number = 42
    agent._cicd_pr_number = 100
    
    try:
        result = agent._build_summary_prompt("old summary", [])
        assert "Worktree path: /some/worktree" in result
        assert "Issue: #42" in result
        assert "PR: #100" in result
    finally:
        agent._cicd_worktree_path = old_wt
        agent._cicd_issue_number = old_issue
        agent._cicd_pr_number = old_pr

def test_setup_logger_log_dir_override(tmp_path):
    # Targets line 1166
    old_log_dir = agent._config.get("log_dir")
    agent._config["log_dir"] = str(tmp_path / "logs")
    try:
        log = agent._setup_logger()
        assert log is not None
    finally:
        if old_log_dir is None:
            agent._config.pop("log_dir", None)
        else:
            agent._config["log_dir"] = old_log_dir
