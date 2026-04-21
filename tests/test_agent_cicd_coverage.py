import pytest
from unittest.mock import MagicMock, patch
import agent
import json
from asyncio import CancelledError

class MockResponse:
    def __init__(self, tool_calls=None, text=None):
        self.status_code = 200
        self.tool_calls = tool_calls
        self.text = text

    def iter_lines(self, decode_unicode=False):
        if self.tool_calls:
            for tc in self.tool_calls:
                payload = {
                    'choices': [{
                        'delta': {
                            'tool_calls': [tc]
                        }
                    }]
                }
                yield f'data: {json.dumps(payload)}'.encode('utf-8')
        elif self.text:
            payload = {
                'choices': [{
                    'delta': {
                        'content': self.text
                    }
                }]
            }
            yield f'data: {json.dumps(payload)}'.encode('utf-8')
        yield b'data: [DONE]'

def test_cancelled_error_during_tool_execution():
    """Test CancelledError during tool execution (lines 2765-2772)"""
    conversation_history = []
    summary_state = {'text': '', 'up_to': 0}
    initial_files = {}
    log = MagicMock()

    tool_call = {
        'index': 0,
        'id': 'call_1',
        'type': 'function',
        'function': {'name': 'exec_command', 'arguments': '{"command": "ls"}'}
    }
    
    with patch('agent._llm_request') as mock_llm, \
         patch('agent._emit'), \
         patch('agent.MAP_FN') as mock_map:
        mock_llm.return_value = MockResponse(tool_calls=[tool_call])
        mock_map.side_effect = CancelledError()
        result = agent.run_agent_single(conversation_history, summary_state, initial_files, log)
        assert result == 'cancelled'

def test_pr_create_guards():
    """Test gh pr create guards (lines 2461-2476)"""
    conversation_history = []
    summary_state = {'text': '', 'up_to': 0}
    initial_files = {}
    log = MagicMock()

    cmd = "gh pr create --title test"
    tool_call = {
        'index': 0,
        'id': 'call_1',
        'type': 'function',
        'function': {'name': 'exec_command', 'arguments': f'{{ "command": "{cmd}" }}'}
    }
    
    with patch('agent._llm_request') as mock_llm, \
         patch('agent._NUDGE_ENABLED', False), \
         patch('agent._emit'):
        mock_llm.side_effect = [
            MockResponse(tool_calls=[tool_call]),
            MockResponse(text='Finished')
        ]
        agent.run_agent_single(conversation_history, summary_state, initial_files, log)

def test_git_worktree_guard():
    """Test git worktree guard (lines 2452-2459)"""
    conversation_history = []
    summary_state = {'text': '', 'up_to': 0}
    initial_files = {}
    log = MagicMock()

    cmd = "git worktree add /tmp/test"
    tool_call = {
        'index': 0,
        'id': 'call_1',
        'type': 'function',
        'function': {'name': 'exec_command', 'arguments': f'{{ "command": "{cmd}" }}'}
    }
    
    with patch('agent._llm_request') as mock_llm, \
         patch('agent._NUDGE_ENABLED', False), \
         patch('agent._emit'):
        mock_llm.side_effect = [
            MockResponse(tool_calls=[tool_call]),
            MockResponse(text='Finished')
        ]
        agent.run_agent_single(conversation_history, summary_state, initial_files, log)
