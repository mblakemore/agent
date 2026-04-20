import pytest
from unittest.mock import MagicMock, patch
import json
from agent import run_agent_single
from tools import MAP_FN

def mock_llm_response(tool_call_args):
    """
    Creates a mock LLM response that triggers a tool call.
    """
    chunks = [
        'data: {"choices": [{"delta": {"content": ""}}]}',
    ]
    tool_call_payload = {
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps(tool_call_args)
                    }
                }]
            }
        }]
    }
    chunks.append(f'data: {json.dumps(tool_call_payload)}')
    chunks.append('data: [DONE]')
    
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [line.encode("utf-8") for line in chunks]
    mock_resp.status_code = 200
    mock_resp.close = MagicMock()
    return mock_resp

def mock_text_response(content):
    """
    Creates a mock LLM response that returns text.
    """
    chunks = [
        f'data: {{"choices": [{{"delta": {{"content": "{content}"}}}}]}}',
        'data: [DONE]'
    ]
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [line.encode("utf-8") for line in chunks]
    mock_resp.status_code = 200
    mock_resp.close = MagicMock()
    return mock_resp

@pytest.mark.parametrize("command, tool_result, expected_warning, description", [
    ('gh issue create --title "Bug" --body "Fix it"', 
     "exit=0\nhttps://github.com/mblakemore/agent/issues/123", 
     "You filed an issue without using the think tool first", 
     "Issue creation without think"),
    ('gh pr create --title "Fix" --body "Fixed bug"', 
     "exit=0\nhttps://github.com/mblakemore/agent/pull/456", 
     "PR #456 was created without a `Closes #<issue>` trailer", 
     "PR creation without Closes trailer"),
    ('gh pr review 456 --approve', 
     "exit=0\nApproved", 
     "You approved a PR without using the think tool first", 
     "PR approval without think"),
    ('gh pr merge 456', 
     "exit=0\nMerged", 
     "You MUST use `gh pr merge --squash --delete-branch`", 
     "PR merge without squash"),
    ('gh pr merge 456 --squash --delete-branch', 
     "exit=1\nError: PR is still a draft", 
     "The PR is still a draft. You must run `gh pr ready <N>` FIRST", 
     "PR merge on draft"),
    ('gh pr merge 456 --squash --delete-branch', 
     "exit=1\nMerged (but we check guard before result)", 
     "You attempted to merge without using the think tool first", 
     "PR merge without think"),
])
def test_cicd_guards_warnings(monkeypatch, command, tool_result, expected_warning, description):
    """Test that specific commands trigger the correct warning injections."""
    mock_exec = MagicMock(return_value=tool_result)
    monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)
    
    with patch('agent._llm_request') as mock_llm:
        mock_llm.side_effect = [
            mock_llm_response({"command": command}),
            mock_text_response("Done")
        ]
        
        history = [{"role": "user", "content": "Test prompt"}]
        mock_log = MagicMock()
        
        # To avoid the PRE-MERGE CHECK blocking the merge command,
        # we must ensure _cicd_issue_view_called is True.
        # Since it's a local variable inside run_agent_single, we can't 
        # easily mock it unless we patch the variable in the scope or
        # simulate the flow. 
        # However, looking at agent.py, we can simulate the flow by 
        # adding a gh issue view call BEFORE the merge call.
        
        if "gh pr merge" in command:
            # If this is a merge test, we need to satisfy the PRE-MERGE CHECK first.
            # We modify the side_effect to include a gh issue view call.
            issue_json = json.dumps({"state": "OPEN", "labels": [{"name": "cicd"}, {"name": "in-progress"}]})
            
            mock_llm.side_effect = [
                mock_llm_response({"command": "gh issue view 1 --json state,labels"}),
                mock_llm_response({"command": command}),
                mock_text_response("Done")
            ]
            
            # Update mock_exec to handle both calls
            mock_exec.side_effect = [f"exit=0\n{issue_json}", tool_result]
        
        with patch('agent._check_api_health', return_value=(True, "ok")), \
             patch('agent._setup_logger'), \
             patch('agent._detect_ctx_size', return_value=None):
            
            run_agent_single(
                conversation_history=history,
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=mock_log
            )
        
        history_str = "".join([str(m) for m in history])
        assert expected_warning in history_str, f"Failed: {description}"

def test_pre_merge_check_flow(monkeypatch):
    """Test the sequence of gh issue view -> gh pr merge."""
    mock_exec = MagicMock()
    monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)
    
    with patch('agent._llm_request') as mock_llm:
        mock_llm.side_effect = [
            mock_llm_response({"command": "gh issue view 1 --json state,labels"}),
            mock_llm_response({"command": "gh pr merge 2 --squash --delete-branch"}),
            mock_text_response("Done")
        ]
        
        issue_json = json.dumps({
            "state": "OPEN",
            "labels": [{"name": "cicd"}, {"name": "in-progress"}]
        })
        mock_exec.side_effect = [f"exit=0\n{issue_json}", "exit=0\nMerged"]
        
        history = [{"role": "user", "content": "Merge issue 1"}]
        mock_log = MagicMock()
        with patch('agent._check_api_health', return_value=(True, "ok")), \
             patch('agent._setup_logger'), \
             patch('agent._detect_ctx_size', return_value=None):
            
            run_agent_single(
                conversation_history=history,
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=mock_log
            )
        
        history_str = "".join([str(m) for m in history])
        assert "PRE-MERGE CHECK SKIPPED" not in history_str

def test_pre_merge_check_failure(monkeypatch):
    """Test that merging without a successful issue view triggers a warning."""
    mock_exec = MagicMock(return_value="exit=0\nMerged")
    monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)
    
    with patch('agent._llm_request') as mock_llm:
        mock_llm.side_effect = [
            mock_llm_response({"command": "gh pr merge 2 --squash --delete-branch"}),
            mock_text_response("Done")
        ]
        
        history = [{"role": "user", "content": "Merge now"}]
        mock_log = MagicMock()
        with patch('agent._check_api_health', return_value=(True, "ok")), \
             patch('agent._setup_logger'), \
             patch('agent._detect_ctx_size', return_value=None):
            
            run_agent_single(
                conversation_history=history,
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=mock_log
            )
        
        history_str = "".join([str(m) for m in history])
        assert "PRE-MERGE CHECK SKIPPED" in history_str
