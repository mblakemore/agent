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
    
    # Make budget very small to force dropping the first user message from 'selected'
    # but leave it in 'conversation_history' for the fallback loop.
    with patch('agent._estimate_tools_tokens', return_value=100), \
         patch('agent._estimate_tokens', side_effect=[10, 10, 10]):
        
        # Force effective_max to be 2, so only Response 1 and 2 are selected initially
        # Wait, let's just use a small ctx_size to force truncation.
        # budget = ctx_size - _TOOLS_TOKENS - reserved_output - max(512, ctx_size // 4)
        # If ctx_size = 1024, reserved_output = 512, TOOLS=100, margin=512 -> budget is negative.
        # Let's just use max_messages_override=1.
        
        selected, oldest_idx = agent._build_context(
            conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
            max_messages_override=2
        )
        
        # selected should be [Response 1, Response 2]
        # fallback should insert [Initial request]
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
    
    with patch('agent._estimate_tools_tokens', return_value=100), \
         patch('agent._estimate_tokens', return_value=10):
        
        selected, oldest_idx = agent._build_context(
            conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
            max_messages_override=1
        )
        
        # No user message to fallback to, selected should just contain the assistant msg
        assert len(selected) == 1
        assert selected[0]["role"] == "assistant"
