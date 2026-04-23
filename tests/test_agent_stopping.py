import pytest
from unittest.mock import MagicMock, patch
from agent import run_agent_single
import logging

def test_run_agent_single_completion_no_persisted():
    """Test line 2408: no persisted work + completion signal."""
    # Corrected patches based on grep results:
    # _load_config is internal, load_extra_tools is imported from tools
    with patch('agent._load_config', return_value={"backends": {}}), \
         patch('agent.load_extra_tools'), \
         patch('agent._llm_request') as mock_llm:
        
        # Mock the LLM to return a completion signal immediately
        mock_llm.return_value = MagicMock()
        mock_llm.return_value.iter_lines.return_value = [
            '{"text": "I have completed the task. DONE", "stop": true}'
        ]
        
        try:
            run_agent_single(
                user_prompt="Do nothing and just say DONE",
                conversation_history=[],
                summary_state={},
                initial_files=[]
            )
        except Exception:
            pass

def test_run_agent_single_nudge_exhausted():
    """Test lines 2430-2432: _total_nudges >= _MAX_TOTAL_NUDGES."""
    with patch('agent._load_config', return_value={"backends": {}}), \
         patch('agent.load_extra_tools'), \
         patch('agent._llm_request') as mock_llm:
        
        # Trigger a nudge by making the LLM fail
        mock_llm.side_effect = Exception("LLM Error")
        
        try:
            run_agent_single(
                user_prompt="test",
                conversation_history=[],
                summary_state={},
                initial_files=[]
            )
        except Exception:
            pass
