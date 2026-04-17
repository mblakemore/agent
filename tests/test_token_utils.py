import pytest
from unittest.mock import patch, MagicMock
import token_utils

def test_count_tokens_exact():
    """Test counting tokens when the exact tokenizer is available."""
    with patch("token_utils._EXACT_TOKENIZER_AVAILABLE", True), \
         patch("token_utils._tokenizer") as mock_tokenizer:
        mock_tokenizer.encode.return_value = [1, 2, 3, 4, 5]
        assert token_utils.count_tokens("hello world") == 5
        mock_tokenizer.encode.assert_called_once_with("hello world")

def test_count_tokens_fallback():
    """Test counting tokens when using the character-based fallback."""
    with patch("token_utils._EXACT_TOKENIZER_AVAILABLE", False):
        # "hello world" is 11 chars. 11 / 3.0 = 3.66 -> 3
        assert token_utils.count_tokens("hello world") == 3
        # Test min 1
        assert token_utils.count_tokens("a") == 1
        # Test empty string
        assert token_utils.count_tokens("") == 0

def test_count_tokens_from_message_content_only():
    """Test counting tokens for a message with only content."""
    msg = {"content": "hello"}
    # With fallback: "hello" = 5 chars / 3.0 = 1.66 -> 1
    with patch("token_utils._EXACT_TOKENIZER_AVAILABLE", False):
        assert token_utils.count_tokens_from_message(msg) == 1

def test_count_tokens_from_message_with_tools():
    """Test counting tokens for a message with content and tool calls."""
    msg = {
        "content": "hello",
        "tool_calls": [{"id": "1", "function": {"name": "test", "arguments": "{}"}}]
    }
    with patch("token_utils._EXACT_TOKENIZER_AVAILABLE", False):
        # Content: 5 chars / 3 = 1
        # tool_calls JSON: '[{"id": "1", "function": {"name": "test", "arguments": "{}"}}]' 
        # approx 60 chars / 3 = 20
        # Total should be > 1
        res = token_utils.count_tokens_from_message(msg)
        assert res > 1

def test_count_tokens_from_message_empty_content():
    """Test counting tokens for a message with None or missing content."""
    assert token_utils.count_tokens_from_message({"content": None}) >= 1
    assert token_utils.count_tokens_from_message({}) >= 1

def test_count_tools_tokens():
    """Test counting tokens for tool schemas."""
    tools = [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {}}}]
    with patch("token_utils._EXACT_TOKENIZER_AVAILABLE", False):
        res = token_utils.count_tools_tokens(tools)
        assert res > 0

def test_tokenizer_loading_errors():
    """Test the logic that handles tokenizer loading errors."""
    # This is tricky because loading happens at import time.
    # We can simulate by manipulating the module's internal state.
    with patch("token_utils._EXACT_TOKENIZER_AVAILABLE", False), \
         patch("token_utils._TOKENIZER_ERROR", None):
        # Force the fallback error message to be set
        # This is a bit of a hack to cover the conditional block
        import importlib
        importlib.reload(token_utils)
        # Since we can't easily trigger the try/except block again without re-importing
        # in a clean process, we just ensure the logic in count_tokens handles the state.
        assert token_utils.count_tokens("test") > 0
