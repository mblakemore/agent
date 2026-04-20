import pytest
from unittest.mock import patch, MagicMock
from agent import _pick_model_interactive

def test_pick_model_interactive_success():
    """Test successful model selection."""
    with patch('agent._list_available_models') as mock_list, \
         patch('agent.input', return_value='1'), \
         patch('agent._emit') as mock_emit:
        
        mock_list.return_value = ['gpt-4', 'claude-3']
        
        result = _pick_model_interactive('gpt-4', 'http://api.test')
        
        assert result == 'gpt-4'
        mock_emit.assert_any_call("on_notice", "info", "Available models at http://api.test:")

def test_pick_model_interactive_invalid_index():
    """Test selection of an out-of-bounds index."""
    with patch('agent._list_available_models') as mock_list, \
         patch('agent.input', return_value='5'), \
         patch('agent._emit') as mock_emit:
        
        mock_list.return_value = ['gpt-4', 'claude-3']
        
        result = _pick_model_interactive('gpt-4', 'http://api.test')
        
        assert result is None
        # We can't easily check for theme.c output, so we just check that it was called
        assert mock_emit.call_args_list[-1][0][0] == "on_notice"
        assert mock_emit.call_args_list[-1][0][1] == "warn"

def test_pick_model_interactive_value_error():
    """Test selection of non-numeric input."""
    with patch('agent._list_available_models') as mock_list, \
         patch('agent.input', return_value='abc'), \
         patch('agent._emit') as mock_emit:
        
        mock_list.return_value = ['gpt-4', 'claude-3']
        
        result = _pick_model_interactive('gpt-4', 'http://api.test')
        
        assert result is None
        assert mock_emit.call_args_list[-1][0][0] == "on_notice"
        assert mock_emit.call_args_list[-1][0][1] == "warn"

def test_pick_model_interactive_cancel():
    """Test canceling selection (blank input)."""
    with patch('agent._list_available_models') as mock_list, \
         patch('agent.input', return_value=''), \
         patch('agent._emit'):
        
        mock_list.return_value = ['gpt-4', 'claude-3']
        
        result = _pick_model_interactive('gpt-4', 'http://api.test')
        
        assert result is None

def test_pick_model_interactive_no_models():
    """Test case where no models are returned from the API."""
    with patch('agent._list_available_models') as mock_list, \
         patch('agent._emit') as mock_emit:
        
        mock_list.return_value = []
        
        result = _pick_model_interactive('gpt-4', 'http://api.test')
        
        assert result is None
        assert mock_emit.call_args_list[0][0][0] == "on_notice"
        assert mock_emit.call_args_list[0][0][1] == "warn"

def test_pick_model_interactive_interrupt():
    """Test KeyboardInterrupt during input."""
    with patch('agent._list_available_models') as mock_list, \
         patch('agent.input', side_effect=KeyboardInterrupt), \
         patch('agent._emit') as mock_emit:
        
        mock_list.return_value = ['gpt-4', 'claude-3']
        
        result = _pick_model_interactive('gpt-4', 'http://api.test')
        
        assert result is None
        assert mock_emit.call_args_list[-1][0][0] == "on_notice"
        assert mock_emit.call_args_list[-1][0][1] == "info"
