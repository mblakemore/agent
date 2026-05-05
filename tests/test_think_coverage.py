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
        self.assertIn("Error: calling server", result)

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_http_error(self, mock_post, mock_url):
        """Test think.fn when response.raise_for_status() fails."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_post.return_value = mock_response

        result = think_mod.fn("test", depth="brief")
        self.assertIn("Error: calling server", result)

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

    def test_empty_prompt_returns_error_without_http_call(self):
        """think.fn with an empty prompt must return an error immediately, no HTTP call."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("", depth="brief")
        self.assertIn("Error: prompt must be a non-empty string", result)
        mock_post.assert_not_called()

    def test_whitespace_only_prompt_returns_error_without_http_call(self):
        """think.fn with a whitespace-only prompt must return an error immediately, no HTTP call."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("   ", depth="brief")
        self.assertIn("Error: prompt must be a non-empty string", result)
        mock_post.assert_not_called()

    def test_non_string_prompt_int_returns_error_without_http_call(self):
        """think.fn with an integer prompt must return a type-specific error (#897)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn(42, depth="brief")  # type: ignore[arg-type]
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("int", result, f"Error must name the bad type: {result!r}")
        self.assertNotIn("non-empty", result, f"'non-empty' is misleading for wrong-type inputs: {result!r}")
        mock_post.assert_not_called()

    def test_non_string_prompt_none_returns_error_without_http_call(self):
        """think.fn with None prompt must return a type-specific error (#897)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn(None, depth="brief")  # type: ignore[arg-type]
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("NoneType", result, f"Error must name the bad type: {result!r}")
        self.assertNotIn("non-empty", result, f"'non-empty' is misleading for wrong-type inputs: {result!r}")
        mock_post.assert_not_called()

    def test_non_string_prompt_list_returns_error_without_http_call(self):
        """think.fn with a list prompt must return a type-specific error (#897)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn(["a", "b"], depth="brief")  # type: ignore[arg-type]
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("list", result, f"Error must name the bad type: {result!r}")
        self.assertNotIn("non-empty", result, f"'non-empty' is misleading for wrong-type inputs: {result!r}")
        mock_post.assert_not_called()

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

    def test_null_byte_in_prompt_returns_error_without_http_call(self):
        """think.fn with a null byte in prompt must return an error, no HTTP call."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("think\x00this", depth="brief")
        self.assertIn("Error", result)
        self.assertIn("null byte", result)
        mock_post.assert_not_called()

    def test_null_byte_in_context_returns_error_without_http_call(self):
        """think.fn with a null byte in context must return an error, no HTTP call."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("valid prompt", depth="brief", context="ctx\x00null")
        self.assertIn("Error", result)
        self.assertIn("null byte", result)
        mock_post.assert_not_called()

    # ── Regression: error message format ──────────────────────────────────────

    @patch("tools.think._get_base_url")
    @patch("requests.post")
    def test_fn_server_error_has_error_prefix(self, mock_post, mock_url):
        """Server error must return 'Error: calling server: ...' (not 'Error calling server: ...')."""
        mock_url.return_value = "http://127.0.0.1:8080"
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")
        result = think_mod.fn("test prompt", depth="brief")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("calling server", result)

    # ── Non-string context type validation (#907) ──────────────────────────────

    def test_non_string_context_int_returns_type_error(self):
        """context=42 must return a type-specific error rather than silently
        convert to empty string (#907)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", depth="brief", context=42)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("int", result, f"Error must name the bad type: {result!r}")
        mock_post.assert_not_called()

    def test_non_string_context_none_treated_as_empty(self):
        """context=None must be treated as '' (not a type error) — same as description=None in task_tracker (#936)."""
        with patch("tools.think._get_base_url", return_value="http://127.0.0.1:8080"):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.raise_for_status.return_value = None
                mock_resp.iter_lines.return_value = [
                    b'data: {"choices": [{"delta": {"content": "ok"}}]}',
                    b"data: [DONE]",
                ]
                mock_post.return_value = mock_resp
                result = think_mod.fn("test prompt", depth="brief", context=None)
        self.assertFalse(result.startswith("Error:"), f"context=None must not return a type error: {result!r}")

    def test_non_string_context_list_returns_type_error(self):
        """context=[] must return a type-specific error (#907)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", depth="brief", context=[])
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("list", result, f"Error must name the bad type: {result!r}")
        mock_post.assert_not_called()

    def test_empty_string_context_still_works(self):
        """context='' (empty string) must still be accepted — empty string means no context (#907)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("valid prompt", depth="brief", context="")
        # Empty string context is valid; the test verifies no type error fires.
        # We don't assert about success here since the mock may not be wired for HTTP response,
        # but we check there is no type-related error.
        self.assertNotIn("must be a string", result,
                         f"Empty string context must not trigger type error: {result!r}")

    # ── Regression: unhashable depth type (#839) ─────────────────────────────

    def test_depth_list_returns_error_without_http_call(self):
        """think.fn with a list depth must return a clean error, not raise TypeError (#839).

        Before the fix, `depth not in DEPTH_MAX_TOKENS` raised
        `TypeError: unhashable type: 'list'` because dict.__contains__
        requires a hashable key.
        """
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", depth=["brief"])
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string' for list depth: {result!r}")
        self.assertIn("'list'", result, f"Error must name the type: {result!r}")
        mock_post.assert_not_called()

    def test_depth_dict_returns_error_without_http_call(self):
        """think.fn with a dict depth must return a type-specific error, not 'invalid depth' (#923)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", depth={})
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string' for dict depth: {result!r}")
        self.assertIn("'dict'", result, f"Error must name the type: {result!r}")
        mock_post.assert_not_called()

    def test_depth_integer_returns_error_without_http_call(self):
        """think.fn with an integer depth must return a type-specific error, not 'invalid depth' (#923)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", depth=1)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string' for int depth: {result!r}")
        self.assertIn("'int'", result, f"Error must name the type: {result!r}")
        mock_post.assert_not_called()

    def test_depth_none_coerces_to_brief(self):
        """think.fn with None depth coerces to 'brief' — no longer a type error (#972 supersedes #923)."""
        with patch("tools.think._get_base_url", return_value="http://127.0.0.1:8080"):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.raise_for_status.return_value = None
                mock_resp.iter_lines.return_value = [
                    b'data: {"choices": [{"delta": {"content": "ok"}}]}',
                    b"data: [DONE]",
                ]
                mock_post.return_value = mock_resp
                result = think_mod.fn("test prompt", depth=None)
        self.assertIsInstance(result, str)
        self.assertFalse(result.startswith("Error:"), f"depth=None must not error: {result!r}")
        self.assertNotIn("NoneType", result, f"NoneType must not appear in result: {result!r}")

    def test_depth_error_message_lists_valid_depths(self):
        """Error message for invalid string depth must list the valid options (#839)."""
        with patch("requests.post"):
            result = think_mod.fn("test prompt", depth="turbo")
        self.assertIn("brief", result)
        self.assertIn("normal", result)
        self.assertIn("deep", result)


# ── context=None treated as empty string (#936) ────────────────────────────────

class TestThinkContextNoneHandling(unittest.TestCase):
    """context=None must be treated as '' not rejected as a type error (#936)."""

    def test_context_none_does_not_return_type_error(self):
        """context=None must be silently treated as '' consistent with task_tracker's description=None (#936)."""
        with patch("tools.think._get_base_url", return_value="http://127.0.0.1:8080"):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.raise_for_status.return_value = None
                mock_resp.iter_lines.return_value = [
                    b'data: {"choices": [{"delta": {"content": "ok"}}]}',
                    b"data: [DONE]",
                ]
                mock_post.return_value = mock_resp
                result = think_mod.fn("test prompt", context=None)
        self.assertFalse(result.startswith("Error:"), f"context=None must not return an error: {result!r}")

    def test_context_integer_still_returns_type_error(self):
        """context=42 must still return a clear type error (only None is special-cased) (#936)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", context=42)
        self.assertTrue(result.startswith("Error:"), f"context=42 must return an error: {result!r}")
        self.assertIn("'int'", result, f"Error must quote type name: {result!r}")
        mock_post.assert_not_called()

    def test_context_list_still_returns_type_error(self):
        """context=['x'] must still return a clear type error (#936)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", context=["context"])
        self.assertTrue(result.startswith("Error:"), f"context=['x'] must return an error: {result!r}")
        self.assertIn("'list'", result, f"Error must quote type name: {result!r}")
        mock_post.assert_not_called()


# ── depth=None treated as "brief" (#972) ──────────────────────────────────────

class TestThinkDepthNoneHandling(unittest.TestCase):
    """depth=None must coerce to 'brief', not return a cryptic type error (#972)."""

    def test_depth_none_does_not_return_type_error(self):
        """depth=None must coerce to 'brief' and proceed normally (#972)."""
        with patch("tools.think._get_base_url", return_value="http://127.0.0.1:8080"):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.raise_for_status.return_value = None
                mock_resp.iter_lines.return_value = [
                    b'data: {"choices": [{"delta": {"content": "ok"}}]}',
                    b"data: [DONE]",
                ]
                mock_post.return_value = mock_resp
                result = think_mod.fn("test prompt", depth=None)
        self.assertFalse(result.startswith("Error:"), f"depth=None must not return an error: {result!r}")
        self.assertNotIn("NoneType", result, f"NoneType must not appear in result: {result!r}")

    def test_depth_none_no_nonetyep_in_error(self):
        """depth=None must not produce 'NoneType' in any output (#972)."""
        with patch("tools.think._get_base_url", return_value="http://127.0.0.1:8080"):
            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.raise_for_status.return_value = None
                mock_resp.iter_lines.return_value = [b"data: [DONE]"]
                mock_post.return_value = mock_resp
                result = think_mod.fn("test prompt", depth=None)
        self.assertNotIn("NoneType", result)

    def test_depth_integer_still_returns_type_error(self):
        """depth=3 must still return a clear type error (only None is special-cased) (#972)."""
        with patch("requests.post") as mock_post:
            result = think_mod.fn("test prompt", depth=3)
        self.assertTrue(result.startswith("Error:"), f"depth=3 must return an error: {result!r}")
        self.assertIn("'int'", result, f"Error must quote type name: {result!r}")
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
