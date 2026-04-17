import unittest
from unittest.mock import MagicMock, patch
import tool_recovery

class TestToolRecoveryCoverage(unittest.TestCase):
    def setUp(self):
        self.mock_log = MagicMock()
        self.mock_config = {'llm': {'model': 'test-model'}}

    def test_no_match(self):
        """Test case where no pattern matches the error."""
        result = tool_recovery.attempt_recovery(
            'file', {'path': 'a.txt'}, 'Unexpected error', {}, MagicMock(), self.mock_config, self.mock_log
        )
        self.assertIsNone(result)

    def test_auto_read_success(self):
        """Test auto_read_first recovery path."""
        tool_name = 'file'
        func_args = {'path': 'test.txt', 'action': 'write'}
        error_str = 'exists but has not been read this session'
        
        mock_map_fn = {
            'file': MagicMock(side_effect=['Read success', 'Write success'])
        }
        
        result = tool_recovery.attempt_recovery(
            tool_name, func_args, error_str, mock_map_fn, MagicMock(), self.mock_config, self.mock_log
        )
        self.assertEqual(result, 'Write success')
        self.assertEqual(mock_map_fn['file'].call_count, 2)

    def test_auto_read_missing_path(self):
        """Test auto_read_first fails because path is missing."""
        result = tool_recovery.attempt_recovery(
            'file', {}, 'exists but has not been read this session', {}, MagicMock(), self.mock_config, self.mock_log
        )
        self.assertIsNone(result)

    def test_auto_read_exception(self):
        """Test auto_read_first fails because read raises exception."""
        mock_map_fn = {'file': MagicMock(side_effect=Exception('Disk error'))}
        result = tool_recovery.attempt_recovery(
            'file', {'path': 'test.txt'}, 'exists but has not been read this session', mock_map_fn, MagicMock(), self.mock_config, self.mock_log
        )
        self.assertIsNone(result)

    def test_auto_read_retry_error_string(self):
        """Test auto_read_first retries but still gets an 'Error' string."""
        mock_map_fn = {'file': MagicMock(side_effect=['Read success', 'Error: still fails'])}
        result = tool_recovery.attempt_recovery(
            'file', {'path': 'test.txt'}, 'exists but has not been read this session', mock_map_fn, MagicMock(), self.mock_config, self.mock_log
        )
        self.assertIsNone(result)

    def test_auto_read_retry_exception(self):
        """Test auto_read_first retries but raises exception."""
        mock_map_fn = {'file': MagicMock(side_effect=['Read success', Exception('Write error')])}
        result = tool_recovery.attempt_recovery(
            'file', {'path': 'test.txt'}, 'exists but has not been read this session', mock_map_fn, MagicMock(), self.mock_config, self.mock_log
        )
        self.assertIsNone(result)

    def test_llm_recovery_format_fail(self):
        """Test LLM recovery when format variables are missing (triggers fallback)."""
        pattern = {
            'pattern': r'start_line (\d+) > end_line (\d+)',
            'tool': 'file',
            'param': 'end_line',
            'question': 'Question {missing_var}',
            'parse': r'(\d+)',
            'type': int
        }
        with patch('tool_recovery.RECOVERY_PATTERNS', [pattern]):
            def mock_llm_call(**kwargs):
                resp = MagicMock()
                resp.json.return_value = {'choices': [{'message': {'content': '10'}}]}
                return resp
            
            # Missing 'missing_var' in func_args
            result = tool_recovery._ask_for_param(pattern, {}, 'error', mock_llm_call, self.mock_config, self.mock_log)
            self.assertEqual(result, 10)

    def test_llm_recovery_empty_response(self):
        """Test LLM recovery when LLM returns empty content."""
        pattern = {'param': 'p', 'question': 'Q', 'parse': r'(\d+)', 'type': int}
        def mock_llm_call(**kwargs):
            resp = MagicMock()
            resp.json.return_value = {'choices': [{'message': {'content': ''}}]}
            return resp
        
        result = tool_recovery._ask_for_param(pattern, {}, 'error', mock_llm_call, self.mock_config, self.mock_log)
        self.assertIsNone(result)

    def test_llm_recovery_parse_fail(self):
        """Test LLM recovery when response cannot be parsed."""
        pattern = {'param': 'p', 'question': 'Q', 'parse': r'(\d+)', 'type': int}
        def mock_llm_call(**kwargs):
            resp = MagicMock()
            resp.json.return_value = {'choices': [{'message': {'content': 'not a number'}}]}
            return resp
        
        result = tool_recovery._ask_for_param(pattern, {}, 'error', mock_llm_call, self.mock_config, self.mock_log)
        self.assertIsNone(result)

    def test_llm_recovery_exception(self):
        """Test LLM recovery when LLM call raises exception."""
        pattern = {'param': 'p', 'question': 'Q', 'parse': r'(\d+)', 'type': int}
        def mock_llm_call(**kwargs):
            raise Exception('API Down')
        
        result = tool_recovery._ask_for_param(pattern, {}, 'error', mock_llm_call, self.mock_config, self.mock_log)
        self.assertIsNone(result)

    def test_llm_recovery_retry_loop_success(self):
        """Test LLM recovery that succeeds on the second attempt."""
        tool_name = 'file'
        func_args = {'start_line': 10, 'end_line': 5}
        error_str = 'start_line (10) > end_line (5)'
        
        call_count = {'llm': 0, 'tool': 0}
        def mock_llm_call(**kwargs):
            call_count['llm'] += 1
            resp = MagicMock()
            resp.json.return_value = {'choices': [{'message': {'content': '15'}}]}
            return resp
            
        def mock_tool_fn(**kwargs):
            call_count['tool'] += 1
            if call_count['tool'] == 1:
                return 'Error: still wrong'
            return 'Success'

        mock_map_fn = {tool_name: mock_tool_fn}
        
        result = tool_recovery.attempt_recovery(
            tool_name, func_args, error_str, mock_map_fn, mock_llm_call, self.mock_config, self.mock_log
        )
        self.assertEqual(result, 'Success')
        self.assertEqual(call_count['llm'], 2)
        self.assertEqual(call_count['tool'], 2)

    def test_llm_recovery_retry_exception(self):
        """Test LLM recovery where retry tool call raises exception."""
        tool_name = 'file'
        func_args = {'start_line': 10, 'end_line': 5}
        error_str = 'start_line (10) > end_line (5)'
        
        def mock_llm_call(**kwargs):
            resp = MagicMock()
            resp.json.return_value = {'choices': [{'message': {'content': '15'}}]}
            return resp
            
        def mock_tool_fn(**kwargs):
            raise Exception('Crash')

        mock_map_fn = {tool_name: mock_tool_fn}
        
        result = tool_recovery.attempt_recovery(
            tool_name, func_args, error_str, mock_map_fn, mock_llm_call, self.mock_config, self.mock_log
        )
        self.assertIsNone(result)

    def test_llm_recovery_max_attempts_exceeded(self):
        """Test LLM recovery that exceeds max attempts by always returning Error."""
        tool_name = 'file'
        func_args = {'start_line': 10, 'end_line': 5}
        error_str = 'start_line (10) > end_line (5)'
        
        def mock_llm_call(**kwargs):
            resp = MagicMock()
            resp.json.return_value = {'choices': [{'message': {'content': '15'}}]}
            return resp
            
        def mock_tool_fn(**kwargs):
            return 'Error: still wrong'

        mock_map_fn = {tool_name: mock_tool_fn}
        
        result = tool_recovery.attempt_recovery(
            tool_name, func_args, error_str, mock_map_fn, mock_llm_call, self.mock_config, self.mock_log
        )
        self.assertIsNone(result)
