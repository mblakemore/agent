import unittest
from unittest.mock import patch, MagicMock
import agent

class TestModelSelection(unittest.TestCase):
    @patch('agent._list_available_models')
    @patch('agent._emit')
    @patch('builtins.input')
    def test_model_selection_valid(self, mock_input, mock_emit, mock_list):
        mock_list.return_value = ['gpt-4', 'gpt-3.5-turbo', 'claude-2']
        mock_input.return_value = '2'
        
        result = agent._pick_model_interactive('gpt-4', 'http://api.example.com')
        self.assertEqual(result, 'gpt-3.5-turbo')

    @patch('agent._list_available_models')
    @patch('agent._emit')
    @patch('builtins.input')
    def test_model_selection_invalid(self, mock_input, mock_emit, mock_list):
        mock_list.return_value = ['gpt-4', 'gpt-3.5-turbo']
        # First input is invalid ('3'), second is valid ('1')
        # However, the function _pick_model_interactive as written only loops once? 
        # Let me check the source again.
        # "if 0 <= idx < len(models): return models[idx]" 
        # "else: _emit... return None"
        # It does NOT loop. It returns None on invalid.
        mock_input.return_value = '3'
        
        result = agent._pick_model_interactive('gpt-4', 'http://api.example.com')
        self.assertIsNone(result)

    @patch('agent._list_available_models')
    @patch('agent._emit')
    @patch('builtins.input')
    def test_model_selection_empty(self, mock_input, mock_emit, mock_list):
        mock_list.return_value = ['gpt-4']
        mock_input.return_value = ''
        
        result = agent._pick_model_interactive('gpt-4', 'http://api.example.com')
        self.assertIsNone(result)

    @patch('agent._list_available_models')
    @patch('agent._emit')
    @patch('builtins.input')
    def test_model_selection_interrupt(self, mock_input, mock_emit, mock_list):
        mock_list.return_value = ['gpt-4']
        mock_input.side_effect = EOFError
        
        result = agent._pick_model_interactive('gpt-4', 'http://api.example.com')
        self.assertIsNone(result)

    @patch('agent._list_available_models')
    @patch('agent._emit')
    def test_model_selection_no_models(self, mock_emit, mock_list):
        mock_list.return_value = []
        
        result = agent._pick_model_interactive('gpt-4', 'http://api.example.com')
        self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()
