import pytest
import requests
from unittest.mock import patch, MagicMock
import agent
from agent import _llm_request, _ReasoningRenderer

def test_llm_request_max_retries_exhausted():
    """Test that _llm_request raises the error after max retries."""
    with patch('requests.post') as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection failed")
        
        # Patch max retries to 1 to speed up test
        with patch('agent._LLM_MAX_RETRIES', 1), \
             patch('agent._emit'), \
             patch('logging.Logger.warning'), \
             patch('time.sleep'):
            
            with pytest.raises(requests.exceptions.ConnectionError):
                # Use a dummy log object
                _llm_request(MagicMock(), kwargs={})

def test_reasoning_renderer_split_tags():
    """Test _ReasoningRenderer with split tags to hit coverage lines."""
    results = []
    renderer = _ReasoningRenderer(lambda x: results.append(x))
    
    # Case 1: Split <think>
    renderer.feed("<th")
    renderer.feed("ink>")
    renderer.feed("Thinking...")
    renderer.feed("</thi")
    renderer.feed("nk>")
    renderer.flush()
    
    # Case 2: Content longer than MAX_PENDING without tags
    results.clear()
    renderer = _ReasoningRenderer(lambda x: results.append(x))
    renderer.feed("A" * 20) # Much larger than 7
    renderer.flush()
    
    assert len(results) > 0

def test_llm_request_500_circuit_breaker():
    """Test that 3 consecutive 500s raise ContextOverflowError."""
    with patch('requests.post') as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_post.return_value = mock_response
        
        with patch('agent._emit'), \
             patch('logging.Logger.warning'), \
             patch('time.sleep'):
            
            with pytest.raises(agent.ContextOverflowError):
                _llm_request(MagicMock(), kwargs={})
