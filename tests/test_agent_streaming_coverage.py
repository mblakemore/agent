import pytest
from agent import run_agent_single, CancelledError
from unittest.mock import MagicMock, patch
import json
import logging
import time

def test_streaming_deadline_exceeded():
    """
    Test that streaming wall-clock deadline is handled (lines 2263-2265).
    """
    mock_response = MagicMock()
    mock_chunks = [{"choices": [{"delta": {"content": "Hello"}}]}]
    
    with patch('time.monotonic') as mock_time:
        # Start time, then a time > start + 600
        mock_time.side_effect = [100.0, 701.0, 702.0, 703.0, 704.0, 705.0]
        
        with patch('agent._llm_request', return_value=mock_response):
            with patch('agent._iter_stream_chunks', return_value=iter(mock_chunks)):
                with patch('agent._safe_close') as mock_safe_close, \
                     patch('agent._emit') as mock_emit:
                    
                    history = [{"role": "user", "content": "Hello"}]
                    summary_state = {"text": "", "up_to": 0}
                    initial_files = {}
                    log = logging.getLogger("test")
                    
                    try:
                        run_agent_single(history, summary_state, initial_files, log)
                    except Exception:
                        pass
                    
                    mock_safe_close.assert_called()

def test_streaming_cancelled_error():
    """
    Test that CancelledError during streaming is handled (lines 2302-2314).
    """
    mock_response = MagicMock()
    
    with patch('agent._llm_request', return_value=mock_response):
        with patch('agent._iter_stream_chunks', side_effect=CancelledError):
            with patch('agent._safe_close') as mock_safe_close, \
                 patch('agent._emit') as mock_emit:
                
                history = [{"role": "user", "content": "Hello"}]
                summary_state = {"text": "", "up_to": 0}
                initial_files = {}
                log = logging.getLogger("test")
                
                try:
                    run_agent_single(history, summary_state, initial_files, log)
                except Exception:
                    pass
                
                # Search for on_cancelled in all calls
                found_cancelled = any(call.args[0] == "on_cancelled" for call in mock_emit.call_args_list)
                assert found_cancelled, "on_cancelled should have been emitted"
                mock_safe_close.assert_called()

def test_streaming_unexpected_error():
    """
    Test that unexpected Exception during streaming is handled (lines 2322-2328).
    """
    mock_response = MagicMock()
    
    with patch('agent._llm_request', return_value=mock_response):
        with patch('agent._iter_stream_chunks', side_effect=RuntimeError("Unexpected")):
            with patch('agent._safe_close') as mock_safe_close, \
                 patch('agent._emit') as mock_emit:
                
                history = [{"role": "user", "content": "Hello"}]
                summary_state = {"text": "", "up_to": 0}
                initial_files = {}
                log = logging.getLogger("test")
                
                try:
                    run_agent_single(history, summary_state, initial_files, log)
                except Exception:
                    pass
                
                # Search for on_error in all calls
                found_error = any(call.args[0] == "on_error" for call in mock_emit.call_args_list)
                assert found_error, "on_error should have been emitted"
                mock_safe_close.assert_called()
