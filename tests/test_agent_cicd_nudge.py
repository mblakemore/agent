import pytest
import logging
import json
from unittest.mock import patch, MagicMock
import agent
from agent import run_agent_single

logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("test_agent_cicd_nudge")

def create_mock_response(content=None, tool_calls=None):
    """Helper to create a mock LLM response in SSE format."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    lines = []
    if tool_calls:
        for tc in tool_calls:
            payload = {"choices": [{"delta": {"tool_calls": [tc]}}]}
            lines.append(f"data: {json.dumps(payload)}".encode())
        lines.append(b'data: [DONE]')
    elif content:
        payload = {"choices": [{"delta": {"content": content}}]}
        lines.append(f"data: {json.dumps(payload)}".encode())
        lines.append(b'data: [DONE]')
    else:
        lines.append(b'data: [DONE]')
    mock_resp.iter_lines.return_value = lines
    return mock_resp

_mock_config_side_effect = lambda k: {
    "llm": {"model": "test-model"},
    "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
    "context": {"max_tokens": 4096, "ctx_size": 32768}
}.get(k)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_auto_nudge_missing_pr(mock_config, mock_llm, mock_emit):
    """CICD nudge fires with 'PR open' when edits done, no PR, no push yet."""
    mock_config.__getitem__.side_effect = _mock_config_side_effect

    import agent as agent_module
    call_count = [0]

    def llm_with_cicd_state(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            agent_module._cicd_issue_number = 466
            agent_module._cicd_branch = 'cicd/test-branch'
            agent_module._cicd_edited_files = {'agent.py'}
        return create_mock_response(content="I have finished the edits.")

    mock_llm.side_effect = llm_with_cicd_state
    conversation_history = [{"role": "user", "content": "Fix the bug"}]
    summary_state = {"text": "", "up_to": 0}
    with patch('agent._NUDGE_ENABLED', True):
        run_agent_single(conversation_history, summary_state, [], log)

    assert any("PR open" in msg.get("content", "")
               for msg in conversation_history if msg.get("role") == "user")

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_auto_nudge_missing_commit(mock_config, mock_llm, mock_emit):
    """CICD nudge fires with 'commit + push + PR open' when edits done but nothing pushed."""
    mock_config.__getitem__.side_effect = _mock_config_side_effect

    import agent as agent_module
    call_count = [0]

    def llm_with_cicd_state(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            agent_module._cicd_issue_number = 466
            agent_module._cicd_branch = 'cicd/test-branch'
            agent_module._cicd_edited_files = {'agent.py'}
        return create_mock_response(content="I have finished the edits.")

    mock_llm.side_effect = llm_with_cicd_state
    conversation_history = [{"role": "user", "content": "Fix the bug"}]
    summary_state = {"text": "", "up_to": 0}
    with patch('agent._NUDGE_ENABLED', True):
        run_agent_single(conversation_history, summary_state, [], log)

    assert any("commit + push + PR open" in msg.get("content", "")
               for msg in conversation_history if msg.get("role") == "user")

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_auto_nudge_persisted_pr_only(mock_config, mock_llm, mock_emit):
    """CICD nudge fires 'PR open' only when push done (_cycle_persisted=True) but no PR yet."""
    mock_config.__getitem__.side_effect = _mock_config_side_effect

    import agent as agent_module
    call_count = [0]
    push_tool = {"index": 0, "id": "cp1", "function": {
        "name": "exec_command",
        "arguments": '{"command": "git push origin cicd/test-branch"}'
    }}

    def llm_with_push(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            agent_module._cicd_issue_number = 466
            agent_module._cicd_branch = 'cicd/test-branch'
            agent_module._cicd_edited_files = {'agent.py'}
            return create_mock_response(tool_calls=[push_tool])
        return create_mock_response(content="Done.")

    mock_llm.side_effect = llm_with_push
    conversation_history = [{"role": "user", "content": "Fix the bug"}]
    summary_state = {"text": "", "up_to": 0}
    with patch('agent._NUDGE_ENABLED', True), \
         patch.dict('agent.MAP_FN', {"exec_command": lambda **kwargs: "exit=0\nPushed."}):
        run_agent_single(conversation_history, summary_state, [], log)

    assert any("PR open" in msg.get("content", "")
               for msg in conversation_history if msg.get("role") == "user")
