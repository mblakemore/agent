import os
import requests
import pytest
from unittest.mock import patch, MagicMock
from tools.web_fetch import fn

@pytest.fixture
def mock_resp():
    mock = MagicMock()
    mock.text = "Hello World"
    mock.headers = {"content-type": "text/plain"}
    mock.status_code = 200
    mock.encoding = 'utf-8'
    # iter_content returns bytes
    mock.iter_content.side_effect = lambda chunk_size=None: [mock.text.encode('utf-8')]
    # Ensure context manager returns the mock itself
    mock.__enter__.return_value = mock
    return mock

def test_web_fetch_plain_text(mock_resp):
    with patch("requests.get", return_value=mock_resp):
        result = fn("http://example.com/text.txt")
        assert "[Fetched: http://example.com/text.txt" in result
        assert "Hello World" in result
        assert "saved to" in result

def test_web_fetch_json(mock_resp):
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.text = '{"key": "value"}'
    mock_resp.iter_content.side_effect = lambda chunk_size=None: [mock_resp.text.encode('utf-8')]
    with patch("requests.get", return_value=mock_resp):
        result = fn("http://example.com/data.json")
        assert '{"key": "value"}' in result

def test_web_fetch_html(mock_resp):
    mock_resp.headers = {"content-type": "text/html"}
    mock_resp.text = "<html><body><h1>Title</h1><p>Content</p></body></html>"
    mock_resp.iter_content.side_effect = lambda chunk_size=None: [mock_resp.text.encode('utf-8')]
    with patch("requests.get", return_value=mock_resp):
        result = fn("http://example.com/page.html")
        assert "Title" in result
        assert "Content" in result

def test_web_fetch_error():
    with patch("requests.get", side_effect=requests.exceptions.HTTPError("404 Client Error")):
        result = fn("http://example.com/404")
        assert "Error fetching URL" in result

def test_web_fetch_truncated(mock_resp):
    mock_resp.headers = {"content-type": "text/plain"}
    mock_resp.text = "A" * 3000
    mock_resp.iter_content.side_effect = lambda chunk_size=None: [mock_resp.text.encode('utf-8')]
    with patch("requests.get", return_value=mock_resp):
        result = fn("http://example.com/long.txt")
        assert "[... truncated" in result
        assert "3000 chars total" in result

def test_web_fetch_html_exception(mock_resp):
    mock_resp.headers = {"content-type": "text/html"}
    mock_resp.iter_content.side_effect = lambda chunk_size=None: [mock_resp.text.encode('utf-8')]
    with patch("requests.get", return_value=mock_resp), \
         patch("tools.web_fetch.markdownify", side_effect=Exception("MD Error")):
        result = fn("http://example.com/fail.html")
        assert "Hello World" in result # Should fallback to streamed text

def test_web_fetch_max_chars(mock_resp):
    mock_resp.headers = {"content-type": "text/plain"}
    mock_resp.text = "A" * 60000
    mock_resp.iter_content.side_effect = lambda chunk_size=None: [mock_resp.text.encode('utf-8')]
    with patch("requests.get", return_value=mock_resp):
        result = fn("http://example.com/huge.txt")
        assert "50000 chars total" in result

def test_web_fetch_generic_exception_uses_error_prefix():
    """Non-RequestException errors must return 'Error: ...' not 'Unexpected error: ...'."""
    with patch("requests.get", side_effect=ValueError("something went wrong")):
        result = fn("http://example.com/boom")
        assert result.startswith("Error:")
        assert not result.startswith("Unexpected error:")
