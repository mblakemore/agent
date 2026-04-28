import pytest
import logging
import json
import requests
from unittest.mock import patch, MagicMock
import agent
from agent import run_agent_single, CancelledError

# Setup basic logging to avoid noise
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("test_agent_loop")

def create_mock_response(content=None, tool_calls=None, side_effect=None):
    """Helper to create a mock LLM response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    
    if side_effect:
        mock_resp.iter_lines.side_effect = side_effect
    else:
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

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_run_agent_single_direct_answer(mock_config, mock_llm, mock_emit):
    """Test the 'happy path' where the agent gives a direct answer."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(content="This is the answer.")
    conversation_history = [{"role": "user", "content": "What is 1+1?"}]
    summary_state = {"text": "", "up_to": 0}
    run_agent_single(conversation_history, summary_state, [], log)
    assert mock_emit.called

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_run_agent_single_tool_loop(mock_config, mock_llm, mock_emit):
    """Test that the agent detects a tool-call loop."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    tool_call = {"index": 0, "id": "call_1", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}}
    mock_resp = create_mock_response(tool_calls=[tool_call])
    mock_llm.side_effect = [mock_resp, mock_resp, mock_resp, mock_resp, create_mock_response(content="Loop detected!")]
    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}
    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: "No results found."}):
        run_agent_single(conversation_history, summary_state, [], log)
    assert mock_llm.call_count <= 6

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_run_agent_single_error_handling(mock_config, mock_llm, mock_emit):
    """Test that the agent handles LLM request failures gracefully."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.side_effect = requests.exceptions.RequestException("Network Timeout")
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    try:
        run_agent_single(conversation_history, summary_state, [], log)
    except Exception:
        pass
    error_emitted = any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)
    assert error_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_cancelled(mock_config, mock_llm, mock_emit):
    """Test that CancelledError during streaming is handled correctly."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(side_effect=CancelledError("Cancelled"))
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    result = run_agent_single(conversation_history, summary_state, [], log)
    assert result == "cancelled"
    cancelled_emitted = any(args[0] == "on_cancelled" for args, kwargs in mock_emit.call_args_list)
    assert cancelled_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_request_exception(mock_config, mock_llm, mock_emit):
    """Test that RequestException during streaming is handled correctly."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(side_effect=requests.exceptions.RequestException("Connection lost"))
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    run_agent_single(conversation_history, summary_state, [], log)
    error_emitted = any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)
    assert error_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_unexpected_exception(mock_config, mock_llm, mock_emit):
    """Test that a general Exception during streaming is handled correctly."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(side_effect=requests.exceptions.RequestException("Connection lost"))
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    run_agent_single(conversation_history, summary_state, [], log)
    error_emitted = any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)
    assert error_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_call_json_decode_error(mock_config, mock_llm, mock_emit):
    """Test that malformed JSON arguments in tool calls are handled gracefully."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    tool_calls = [
        {"index": 0, "id": "call_valid", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}},
        {"index": 1, "id": "call_garbled", "function": {"name": "search_files", "arguments": '{"pattern": "test"'}},
    ]
    mock_resp = create_mock_response(tool_calls=tool_calls)
    mock_llm.side_effect = [mock_resp, create_mock_response(content="Fixed it!")]
    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}
    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: "No results found."}):
        run_agent_single(conversation_history, summary_state, [], log)
    assert any("malformed arguments" in msg.get("content", "") 
               for msg in conversation_history if msg.get("role") == "tool")

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_call_generic_exception(mock_config, mock_llm, mock_emit):
    """Test that unexpected exceptions during tool call argument parsing are handled gracefully."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    tool_call = {
        "index": 0, 
        "id": "call_2", 
        "function": {"name": "search_files", "arguments": None}
    }
    mock_resp = create_mock_response(tool_calls=[tool_call])
    mock_llm.side_effect = [
        mock_resp, 
        create_mock_response(tool_calls=[{"index": 0, "id": "call_3", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}}]),
        create_mock_response(content="Fixed it!")
    ]
    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}
    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: "No results으로 "}) :
        run_agent_single(conversation_history, summary_state, [], log)
    assert mock_llm.call_count >= 2

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_hallucination_text_only_retry(mock_config, mock_llm, mock_emit):
    """Test that text-only responses are stripped and retried."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    with patch('agent._NUDGE_ENABLED', True):
        # counter=1: on_hallucination_stripped text_only, pop, continue
        # counter=2: nudge appended, continue
        # counter=3 >= _MAX_TEXT_ONLY(3): return "done"
        mock_llm.side_effect = [
            create_mock_response(content="I am thinking..."),
            create_mock_response(content="I am still thinking..."),
            create_mock_response(content="Not sure what to do."),
        ]
        conversation_history = [{"role": "user", "content": "Search for test"}]
        summary_state = {"text": "", "up_to": 0}
        run_agent_single(conversation_history, summary_state, [], log)

    hallucination_emitted = any(args[0] == "on_hallucination_stripped" and args[1] == "text_only"
                               for args, kwargs in mock_emit.call_args_list)
    assert hallucination_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_hallucination_fabricated_read(mock_config, mock_llm, mock_emit):
    """Test detection of fabricated file read claims."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    with patch('agent._NUDGE_ENABLED', True):
        mock_llm.side_effect = [
            create_mock_response(content="I am thinking..."),
            create_mock_response(content="I have successfully read the contents of agent.py and found a bug."),
            create_mock_response(content="I apologize, I will actually read the file now."),
        ]
        conversation_history = [{"role": "user", "content": "Read agent.py"}]
        summary_state = {"text": "", "up_to": 0}
        run_agent_single(conversation_history, summary_state, [], log)

    assert any("You did NOT actually read that file" in msg.get("content", "") 
               for msg in conversation_history if msg.get("role") == "user")
    
    hallucination_emitted = any(args[0] == "on_hallucination_stripped" and args[1] == "file_read" 
                               for args, kwargs in mock_emit.call_args_list)
    assert hallucination_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_auto_nudge_missing_pr(mock_config, mock_llm, mock_emit):
    """Test the CICD auto-nudge when a PR is missing after edits."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    import agent as agent_module
    call_count = [0]
    def llm_with_cicd_state_pr(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            agent_module._cicd_issue_number = 466
            agent_module._cicd_branch = 'cicd/test-branch'
            agent_module._cicd_edited_files = {'agent.py'}
        return create_mock_response(content="I have finished the edits.")
    mock_llm.side_effect = llm_with_cicd_state_pr
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
    """Test the CICD auto-nudge when a commit/push/PR is missing after edits."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
        }
    
    import agent as agent_module
    call_count2 = [0]
    def llm_with_cicd_state_commit(*args, **kwargs):
        call_count2[0] += 1
        if call_count2[0] == 1:
            agent_module._cicd_issue_number = 466
            agent_module._cicd_branch = 'cicd/test-branch'
            agent_module._cicd_edited_files = {'agent.py'}
        return create_mock_response(content="I have finished the edits.")
    mock_llm.side_effect = llm_with_cicd_state_commit
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
    """Test CICD auto-nudge PR-only path: git push done (_cycle_persisted=True), PR still missing."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    import agent as agent_module
    call_count3 = [0]
    push_tool = {"index": 0, "id": "cp1", "function": {
        "name": "exec_command",
        "arguments": '{"command": "git push origin cicd/test-branch"}'
    }}

    def llm_with_push(*args, **kwargs):
        call_count3[0] += 1
        if call_count3[0] == 1:
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
