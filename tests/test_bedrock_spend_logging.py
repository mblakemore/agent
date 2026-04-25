import pytest
from unittest.mock import MagicMock, patch
import logging
import agent

def test_log_bedrock_session_spend_no_bedrock():
    """Verify that the function does nothing if backends are not bedrock."""
    mock_log = MagicMock(spec=logging.Logger)
    
    with patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary:
        
        mock_main.kind = 'openai'
        mock_summary.kind = 'openai'
        
        agent._log_bedrock_session_spend(mock_log)
        mock_log.info.assert_not_called()

def test_log_bedrock_session_spend_with_bedrock():
    """Verify that Bedrock spend is logged when the backend is Bedrock."""
    mock_log = MagicMock(spec=logging.Logger)
    
    # Mock Bedrock backend
    mock_backend = MagicMock()
    mock_backend.kind = 'bedrock'
    mock_backend.model = 'anthropic.claude-3-sonnet'
    mock_backend._cfg = {'some': 'config'}
    mock_backend._session_conv_count = 5
    
    with patch('agent._main_backend', mock_backend), \
         patch('agent._summary_backend') as mock_summary, \
         patch('llm_backend._load_today_spend') as mock_load, \
         patch('llm_backend._resolve_daily_cap') as mock_cap:
        
        mock_summary.kind = 'none' # Only main is bedrock
        mock_load.return_value = 1.2345
        mock_cap.return_value = 10.00
        
        agent._log_bedrock_session_spend(mock_log)
        
        # Check for spend log
        mock_log.info.assert_any_call(
            "bedrock.session_spend role=%s model=%s today_usd=%.4f cap_usd=%.2f",
            "main",
            'anthropic.claude-3-sonnet',
            1.2345,
            10.00,
        )
        # Check for conversation count log
        mock_log.info.assert_any_call(
            "bedrock.session_conv_count role=%s model=%s count=%d",
            "main",
            'anthropic.claude-3-sonnet',
            5,
        )

def test_log_bedrock_session_spend_exception_handling():
    """Verify that exceptions in load/cap don't crash the logger."""
    mock_log = MagicMock(spec=logging.Logger)
    
    mock_backend = MagicMock()
    mock_backend.kind = 'bedrock'
    
    with patch('agent._main_backend', mock_backend), \
         patch('agent._summary_backend') as mock_summary, \
         patch('llm_backend._load_today_spend', side_effect=Exception("API Error")):
        
        mock_summary.kind = 'none'
        
        # Should not raise exception
        agent._log_bedrock_session_spend(mock_log)
        mock_log.info.assert_not_called()

def test_log_bedrock_session_spend_import_error():
    """Verify that the function returns silently if llm_backend imports fail."""
    mock_log = MagicMock(spec=logging.Logger)
    
    with patch('builtins.__import__', side_effect=ImportError):
        # This is tricky because llm_backend might already be imported.
        # We can mock the try-except block by forcing the import to fail inside the function.
        # Since the import is inside the function:
        with patch('agent._main_backend') as mb:
            mb.kind = 'bedrock'
            # Force a failure during the local import inside the function
            with patch('builtins.__import__', side_effect=Exception("Import Fail")):
                 agent._log_bedrock_session_spend(mock_log)
                 mock_log.info.assert_not_called()
