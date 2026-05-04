import unittest
from unittest.mock import MagicMock, patch, mock_open
import requests
import json
import os

# Ensure we can import tools.think
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.think as think_mod

class TestThinkCoverage(unittest.TestCase):

    def setUp(self):
        # Reset the injectable output to prevent pollution
        self.original_output = think_mod._output
        think_mod._output = MagicMock()

    def tearDown(self):
        think_mod._output = self.original_output

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_success_no_thinking(self, mock_post, mock_url):
        """Test think.fn when the model returns a direct answer without a thinking block."""
        mock_url.return_value = "http://127.0.0.1:8080"
        
        # Mock response stream
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        # Return stream: data: {chunk} ... data: [DONE]
        chunks = [
            b'data: {"choices": [{"delta": {"content": "Hello"}}]}',
            b'data: {"choices": [{"delta": {"content": " world!"}}]}',
            b'data: [DONE]',
        ]
        mock_response.iter_lines.return_value = chunks
        mock_post.return_value = mock_response

        result = think_mod.fn("What is 1+1?", depth="brief")
        
        self.assertEqual(result, "Hello world!")
        think_mod._output.assert_called()

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_success_with_thinking(self, mock_post, mock_url):
        """Test think.fn when the model returns a thinking block and an answer."""
        mock_url.return_value = "http://127.0.0.1:8080"
        
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        full_content = "<|channel>thought\nI should add 1 and 1.<channel|>The answer is 2"
        
        chunks = []
        current = ""
        for char in full_content:
            current += char
            if len(current) > 5:
                chunk = "data: " + json.dumps({"choices": [{"delta": {"content": current}}]})
                chunks.append(chunk.encode())
                current = ""
        if current:
            chunk = "data: " + json.dumps({"choices": [{"delta": {"content": current}}]})
            chunks.append(chunk.encode())
        chunks.append(b"data: [DONE]")
        
        mock_response.iter_lines.return_value = chunks
        mock_post.return_value = mock_response
        
        result = think_mod.fn("What is 1+1?", depth="brief")
        
        self.assertEqual(result, "The answer is 2")
        calls = [call.args[0] for call in think_mod._output.call_args_list]
        self.assertTrue(any("I should add 1 and 1" in c for c in calls))

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_request_exception(self, mock_post, mock_url):
        """Test think.fn when a requests exception occurs."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection failed")
        
        result = think_mod.fn("test", depth="brief")
        self.assertIn("Error calling server", result)

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_http_error(self, mock_post, mock_url):
        """Test think.fn when response.raise_for_status() fails."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_post.return_value = mock_response
        
        result = think_mod.fn("test", depth="brief")
        self.assertIn("Error calling server", result)

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_empty_response(self, mock_post, mock_url):
        """Test think.fn when the model returns nothing."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.iter_lines.return_value = [b'data: [DONE]']
        mock_post.return_value = mock_response
        
        result = think_mod.fn("test", depth="brief")
        self.assertEqual(result, "Error: empty response from model")

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_malformed_json(self, mock_post, mock_url):
        """Test think.fn when streaming data contains malformed JSON."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.iter_lines.return_value = [
            b'data: {invalid json}',
            b'data: {"choices": [{"delta": {"content": "Valid"}}]}',
            b'data: [DONE]',
        ]
        mock_post.return_value = mock_response
        
        result = think_mod.fn("test", depth="brief")
        self.assertEqual(result, "Valid")

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_missing_choices(self, mock_post, mock_url):
        """Test think.fn when the JSON payload is missing 'choices'."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.iter_lines.return_value = [
            b'data: {"not_choices": []}',
            b'data: [DONE]',
        ]
        mock_post.return_value = mock_response
        
        result = think_mod.fn("test", depth="brief")
        self.assertEqual(result, "Error: empty response from model")

    def test_invalid_depth_returns_error_without_http_call(self):
        """think.fn with an invalid depth must return an error immediately, no HTTP call."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("x", depth="turbo")
        self.assertIn("Error: invalid depth", result)
        self.assertIn("turbo", result)
        self.assertIn("brief", result)
        self.assertIn("normal", result)
        self.assertIn("deep", result)
        mock_post.assert_not_called()

    def test_get_base_url_default(self):
        """Test _get_base_url when config.json is missing."""
        with patch("os.path.exists", return_value=False):
            self.assertEqual(think_mod._get_base_url(), "http://127.0.0.1:8080")

    def test_get_base_url_with_config(self):
        """Test _get_base_url when config.json exists and has the value."""
        config_data = json.dumps({"llm": {"base_url": "http://custom.url:11434"}})
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=config_data)):
            self.assertEqual(think_mod._get_base_url(), "http://custom.url:11434")

    def test_get_base_url_exception(self):
        """Test _get_base_url when an exception occurs during reading."""
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=Exception("Read error")):
            self.assertEqual(think_mod._get_base_url(), "http://127.0.0.1:8080")

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_with_context(self, mock_post, mock_url):
        """Verify that context is correctly added to the messages."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.iter_lines.return_value = [b'data: [DONE]']
        mock_post.return_value = mock_response
        
        think_mod.fn("prompt", depth="brief", context="some context")
        
        # Check the request body sent to requests.post
        args, kwargs = mock_post.call_args
        request_body = kwargs['json']
        messages = request_body['messages']
        
        # Expected messages: system, user(context), assistant(understood), user(prompt)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[1]['role'], 'user')
        self.assertEqual(messages[1]['content'], 'some context')
        self.assertEqual(messages[2]['role'], 'assistant')
        self.assertEqual(messages[3]['content'], 'prompt')

if __name__ == "__main__":
    unittest.main()
