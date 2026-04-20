import pytest
from unittest.mock import MagicMock, patch
import logging
from agent import run_agent_single, ContextOverflowError

def test_context_overflow_max_attempts():
    """Test that max reduction attempts lead to an error return value."""
    with patch('agent._llm_request') as mock_request:
        # Always raise ContextOverflowError
        mock_request.side_effect = ContextOverflowError()
        
        history = [{"role": "user", "content": "Hello"}]
        # Provide a long summary so it can be truncated multiple times
        summary = {"text": "a" * 2048, "up_to": 0}
        files = {}
        mock_log = MagicMock(spec=logging.Logger)
        
        with patch('agent._emit'), patch('agent._build_context') as mock_build:
            # Return 2 messages so it hits the "current_count <= 2" branch
            mock_build.return_value = ([{"role": "user", "content": "1"}, {"role": "user", "content": "2"}], 0)
            
            result = run_agent_single(history, summary, files, 
                                     log=mock_log,
                                     ctx_size=100, max_tokens=1000, 
                                     temperature=0.7, top_p=1.0, top_k=1.0, 
                                     presence_penalty=0.0)
            
            assert result == "error"
            # _CTX_REDUCE_MAX is 10. Loop is range(_CTX_REDUCE_MAX + 1), so 11 attempts.
            assert mock_request.call_count == 11

def test_context_overflow_summary_truncation():
    """Test that summary is truncated when messages are at minimum."""
    with patch('agent._llm_request') as mock_request:
        # Raise ContextOverflowError
        mock_request.side_effect = ContextOverflowError()
        
        history = [{"role": "user", "content": "Hello"}]
        summary = {"text": "This is a very long summary that should be truncated", "up_to": 0}
        files = {}
        mock_log = MagicMock(spec=logging.Logger)
        
        with patch('agent._emit'), patch('agent._build_context') as mock_build:
            # Return 2 messages to trigger the current_count <= 2 condition
            mock_build.return_value = ([{"role": "user", "content": "1"}, {"role": "user", "content": "2"}], 0)
            
            # We only want to test the truncation, so we can use a side effect to exit
            def side_effect(*args, **kwargs):
                # After the first attempt, we've truncated. Now we stop.
                # We can't just raise an exception because we want to check the result of run_agent_single.
                # But run_agent_single only returns when it succeeds or fails.
                # So let's just let it continue until it returns "error" or we force it.
                raise ContextOverflowError()
            
            mock_request.side_effect = side_effect
            
            # To avoid a long loop in this test, we can mock _CTX_REDUCE_MAX if we could, 
            # but it's a local variable in run_agent_single.
            # Instead, we'll just let it run (it's fast).
            
            result = run_agent_single(history, summary, files, 
                                     log=mock_log,
                                     ctx_size=100, max_tokens=1000, 
                                     temperature=0.7, top_p=1.0, top_k=1.0, 
                                     presence_penalty=0.0)
            
            assert result == "error"
            # Initial len: 54. First truncation: 27. Second: 13...
            # It should be much smaller than 54.
            assert len(summary["text"]) < 54

def test_context_overflow_reduction_step():
    """Test that _ctx_max_messages is decreased on overflow."""
    with patch('agent._llm_request') as mock_request:
        class ExitAgent(Exception): pass
        
        def side_effect(*args, **kwargs):
            if mock_request.call_count == 1:
                raise ContextOverflowError()
            # For the second call, let's just exit so we can inspect the state
            raise ExitAgent()

        mock_request.side_effect = side_effect
        
        history = [{"role": "user", "content": "Hello"}]
        summary = {"text": "Some summary", "up_to": 0}
        files = {}
        mock_log = MagicMock(spec=logging.Logger)
        
        with patch('agent._emit'), patch('agent._build_context') as mock_build:
            # Return a list of 10 messages so that reduction is visible
            mock_build.return_value = ([{"role": "user", "content": str(i)} for i in range(10)], 0)
            
            with pytest.raises(ExitAgent):
                run_agent_single(history, summary, files, 
                                 log=mock_log,
                                 ctx_size=100, max_tokens=1000, 
                                 temperature=0.7, top_p=1.0, top_k=1.0, 
                                 presence_penalty=0.0)
            
            # Check if _build_context was called with a reduced max_messages_override
            # The call is _build_context(conversation_history, summary_state, initial_files, ctx_size, max_tokens, log, max_messages_override=...)
            # Let's inspect all calls to _build_context.
            
            # Call 0: attempt 0 -> max_messages_override=None
            # Call 1: attempt 1 -> max_messages_override should be reduced.
            
            # The call is _build_context(conversation_history, summary_state, initial_files, ctx_size, max_tokens, log, max_messages_override=...)
            # It has 6 positional args and 1 keyword arg max_messages_override.
            # Or maybe all positional.
            
            calls = mock_build.call_args_list
            assert len(calls) >= 2
            
            last_call_args, last_call_kwargs = calls[1]
            max_msgs = last_call_kwargs.get('max_messages_override')
            if max_msgs is None and len(last_call_args) >= 7:
                max_msgs = last_call_args[6]
            
            assert max_msgs is not None
            assert max_msgs == 8 # 10 - 2
