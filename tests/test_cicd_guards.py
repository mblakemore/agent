import os
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
     "Error: CICD gh pr create blocked — the --body must contain `Closes #<N>`", 
     "PR creation without Closes trailer"),
    ('gh pr review 456 --approve', 
     "exit=0\nApproved", 
     "Proceed with merge only after thinking", 
     "PR approval without think"),
    ('gh pr merge 456',
     "exit=0\nMerged",
     "You MUST use `gh pr merge --squash`",
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
        
        history = [{"role": "user", "content": "# CICD Improvement Loop — Builder\nTest prompt"}]
        mock_log = MagicMock()
    
        if "gh pr merge" in command:
            issue_json = json.dumps({"state": "OPEN", "labels": [{"name": "cicd"}, {"name": "in-progress"}]})
            mock_llm.side_effect = [
                mock_llm_response({"command": "gh issue view 1 --json state,labels"}),
                mock_llm_response({"command": command}),
                mock_text_response("Done")
            ]
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

from agent import _handle_cicd_file_edit, _check_worktree_guard

def test_check_worktree_guard():
    """Test that _check_worktree_guard correctly identifies violations."""
    cwd = "/mnt/droid/repos/agent/temp/20260420_192045/repo"
    wt = "/mnt/droid/repos/agent/temp/20260420_192045/worktrees/245-cicd-guards"
    
    with patch('os.getcwd', return_value=cwd):
        path_ok = os.path.join(wt, "tests/test_cicd_guards.py")
        is_violation, correction = _check_worktree_guard(path_ok, wt)
        assert is_violation is False
        assert correction is None
        
        path_bad = os.path.join(cwd, "README.md")
        is_violation, correction = _check_worktree_guard(path_bad, wt)
        assert is_violation is True
        assert correction == os.path.join(wt, "README.md")
        
        path_outside = "/tmp/some_file.txt"
        is_violation, correction = _check_worktree_guard(path_outside, wt)
        assert is_violation is False
        assert correction is None

def test_handle_cicd_file_edit_logic():
    """Test all branches of _handle_cicd_file_edit."""
    mock_log = MagicMock()
    history = []
    wt_path = "/mnt/droid/repos/agent/temp/20260420_192045/worktrees/245-cicd-guards"
    phase_state = {"plan": False}
    edited_files = set()
    
    args = {"path": "file.txt"}
    has_edited, persisted = _handle_cicd_file_edit(
        args, history, wt_path, phase_state, edited_files, False, False, 1, mock_log
    )
    assert has_edited is True
    assert "file.txt" in edited_files
    
    with patch('agent._check_worktree_guard', return_value=(True, "/correct/path")):
        args = {"path": "/bad/path"}
        _handle_cicd_file_edit(
            args, history, wt_path, phase_state, edited_files, True, False, 2, mock_log
        )
        assert any("WRONG PATH!" in m["content"] for m in history if m["role"] == "user")
    
    args = {"path": "improvements/245-slug.md"}
    has_edited, persisted = _handle_cicd_file_edit(
        args, history, wt_path, phase_state, edited_files, True, False, 3, mock_log
    )
    assert phase_state["plan"] is True
    
    args = {"path": "reviews.md"}
    has_edited, persisted = _handle_cicd_file_edit(
        args, history, wt_path, phase_state, edited_files, True, False, 4, mock_log
    )
    assert persisted is True
    
    args = {"path": "src/main.py"}
    has_edited, persisted = _handle_cicd_file_edit(
        args, history, wt_path, phase_state, edited_files, True, True, 5, mock_log
    )
    assert has_edited is True
    assert persisted is True

@pytest.mark.parametrize("issue_json, description", [
    (json.dumps({"state": "CLOSED", "labels": [{"name": "cicd"}, {"name": "in-progress"}]}), 
     "CLOSED issue"),
    (json.dumps({"state": "OPEN", "labels": []}), 
     "missing labels"),
    (json.dumps({"state": "OPEN", "labels": [{"name": "bug"}]}), 
     "wrong labels"),
])
def test_pre_merge_check_invalid_criteria(monkeypatch, issue_json, description):
    """Test that merging with invalid issue criteria triggers a warning."""
    mock_exec = MagicMock()
    monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)
    
    with patch('agent._llm_request') as mock_llm:
        mock_llm.side_effect = [
            mock_llm_response({"command": "gh issue view 1 --json state,labels"}),
            mock_llm_response({"command": "gh pr merge 2 --squash --delete-branch"}),
            mock_text_response("Done")
        ]
        
        mock_exec.side_effect = [f"exit=0\n{issue_json}", "exit=0\nMerged"]
        
        history = [{"role": "user", "content": f"Merge with {description}"}]
        mock_log = MagicMock()
        with patch('agent._check_api_health', return_value=(True, "ok")), \
             patch('agent._setup_logger'), \
             patch('agent._setup_logger'), \
             patch('agent._detect_ctx_size', return_value=None):
            
            run_agent_single(
                conversation_history=history,
                summary_state={"text": "", "up_to": 0},
                initial_files=[],
                log=mock_log
            )
        
        history_str = "".join([str(m) for m in history])
        assert "PRE-MERGE CHECK FAILED" in history_str, f"Failed to detect {description}"

def test_reviewer_file_edit_blocked(monkeypatch):
    """Test that a CICD Reviewer is blocked from editing .py files in their worktree."""
    mock_file = MagicMock(return_value="File written")
    monkeypatch.setitem(MAP_FN, "file", mock_file)
    
    with patch('agent._llm_request') as mock_llm:
        def mock_file_response(args):
            chunks = ['data: {"choices": [{"delta": {"content": ""}}]}']
            payload = {
                "choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "call_123", "type": "function",
                    "function": {"name": "file", "arguments": json.dumps(args)}
                }]}}]
            }
            chunks.append(f'data: {json.dumps(payload)}')
            chunks.append('data: [DONE]')
            resp = MagicMock()
            resp.iter_lines.return_value = [l.encode("utf-8") for l in chunks]
            resp.status_code = 200
            resp.close = MagicMock()
            return resp

        mock_llm.side_effect = [
            mock_file_response({"action": "write", "path": "/mnt/droid/repos/agent/temp/worktrees/pr-123/agent.py"}),
            mock_text_response("Done")
        ]
        
        history = [{"role": "user", "content": "# CICD Improvement Loop — Reviewer\nTest prompt"}]
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
        
        assert any("Error: CICD reviewer file edit BLOCKED" in str(m.get("content", "")) for m in history)

def test_push_to_main_blocked(monkeypatch):
    """Test that a CICD Builder/Reviewer is blocked from pushing directly to main."""
    mock_exec = MagicMock(return_value="exit=0\nPushed")
    monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)
    
    with patch('agent._llm_request') as mock_llm:
        mock_llm.side_effect = [
            mock_llm_response({"command": "git push origin main"}),
            mock_text_response("Done")
        ]
        
        history = [{"role": "user", "content": "# CICD Improvement Loop — Builder\nTest prompt"}]
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
        
        # Flexible check for blocking message
        assert any(
            "Error: CICD" in str(m.get("content", "")) and 
            "git push origin main" in str(m.get("content", "")) and 
            "BLOCKED" in str(m.get("content", "")) 
            for m in history
        ), "Expected block message for push to main was not found"

def test_pre_merge_warning_logged(monkeypatch):
    """Test that a warning is logged when gh pr merge is called without viewing the issue first."""
    mock_exec = MagicMock(return_value="exit=0\nMerged")
    monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)
    
    with patch('agent._llm_request') as mock_llm:
        mock_llm.side_effect = [
            mock_llm_response({"command": "gh pr merge 123 --squash"}),
            mock_text_response("Done")
        ]
        
        history = [{"role": "user", "content": "# CICD Improvement Loop — Builder\nTest prompt"}]
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
        
        calls = mock_log.warning.call_args_list
        assert any("PRE-MERGE CHECK required" in call[0][0] for call in calls), \
            "Warning log for PRE-MERGE CHECK was not found"
