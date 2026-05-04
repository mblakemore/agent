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


def test_web_fetch_empty_url_returns_error():
    """Empty url must be rejected immediately with a clear error, not forwarded to requests."""
    result = fn(url="")
    assert result.startswith("Error:")
    assert "empty" in result.lower()


def test_web_fetch_whitespace_only_url_returns_error():
    """Whitespace-only url must also be rejected with a clear error."""
    result = fn(url="   ")
    assert result.startswith("Error:")
    assert "empty" in result.lower()


def test_web_fetch_no_scheme_url_returns_error():
    """URLs without http:// or https:// scheme must be rejected with a clear, library-free error."""
    result = fn(url="not-a-url")
    assert result.startswith("Error:")
    assert "http" in result
    # Must NOT leak requests' internal suggestion
    assert "Perhaps you meant" not in result


def test_web_fetch_ftp_url_returns_error():
    """Non-HTTP schemes (e.g. ftp://) must be rejected before reaching requests."""
    result = fn(url="ftp://example.com/file.txt")
    assert result.startswith("Error:")
    assert "http" in result


def test_web_fetch_int_url_returns_error():
    """Passing an int as url must return an error string, not raise AttributeError."""
    result = fn(url=42)
    assert isinstance(result, str)
    assert "Error" in result


def test_web_fetch_none_url_returns_error():
    """Passing None as url must return an error string, not raise AttributeError."""
    result = fn(url=None)
    assert isinstance(result, str)
    assert "Error" in result


# ── URL validation edge cases (#770) ──────────────────────────────────────────

def test_web_fetch_bare_hostname_returns_error():
    """A bare hostname without scheme (e.g. 'example.com/page') must be rejected
    with a clear error before reaching the requests library (#770)."""
    result = fn(url="example.com/page")
    assert isinstance(result, str), "fn() must return a string, not raise"
    assert result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}"
    # Must mention that http:// or https:// is required
    assert "http" in result, f"Error must mention http/https requirement: {result!r}"
    # Must NOT fall through to requests and leak a library error
    assert "Perhaps you meant" not in result


def test_web_fetch_ftp_scheme_mentions_supported_schemes():
    """ftp:// must be rejected with a message that names http/https as supported (#770)."""
    result = fn(url="ftp://example.com")
    assert isinstance(result, str)
    assert result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}"
    assert "https" in result or "http" in result, (
        f"Error must mention supported schemes: {result!r}"
    )


def test_web_fetch_http_url_passes_scheme_check():
    """http:// URL must pass the scheme check (network call is mocked to avoid real I/O)."""
    import requests
    from unittest.mock import patch, MagicMock
    mock = MagicMock()
    mock.headers = {"content-type": "text/plain"}
    mock.status_code = 200
    mock.encoding = "utf-8"
    mock.iter_content.side_effect = lambda chunk_size=None: [b"ok"]
    mock.__enter__.return_value = mock
    with patch("requests.get", return_value=mock):
        result = fn(url="http://example.com/test")
    # Must not be a scheme-validation error
    assert "must begin with" not in result, f"Valid http:// URL failed scheme check: {result!r}"


# ── Binary response detection (#784) ─────────────────────────────────────────

def test_web_fetch_binary_image_returns_error():
    """A binary image response (JPEG magic bytes with null bytes) must return an
    error string, not mojibake binary content (#784)."""
    mock = MagicMock()
    mock.headers = {"content-type": "image/jpeg"}
    mock.status_code = 200
    mock.encoding = "utf-8"
    # JPEG starts with FF D8 FF E0 00 (contains null byte)
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 50 + b"data"
    mock.iter_content.side_effect = lambda chunk_size=None: [jpeg_bytes]
    mock.__enter__.return_value = mock
    with patch("requests.get", return_value=mock):
        result = fn(url="https://example.com/photo.jpg")
    assert result.startswith("Error:"), f"Expected 'Error:' prefix for binary response: {result!r}"
    assert "binary" in result.lower(), f"Error must mention 'binary': {result!r}"
    assert "image/jpeg" in result, f"Error must include content-type: {result!r}"


def test_web_fetch_binary_unknown_content_type_returns_error():
    """A binary response with no content-type header must still return an error (#784)."""
    mock = MagicMock()
    mock.headers = {}
    mock.status_code = 200
    mock.encoding = "utf-8"
    # Any bytes with a null byte in the first 8192
    binary_bytes = b"some data\x00more data"
    mock.iter_content.side_effect = lambda chunk_size=None: [binary_bytes]
    mock.__enter__.return_value = mock
    with patch("requests.get", return_value=mock):
        result = fn(url="https://example.com/data.bin")
    assert result.startswith("Error:"), f"Expected 'Error:' for binary with no content-type: {result!r}"
    assert "binary" in result.lower(), f"Error must mention 'binary': {result!r}"


def test_web_fetch_text_with_no_null_bytes_is_not_rejected():
    """A plain text response must not be falsely classified as binary (#784)."""
    mock = MagicMock()
    mock.headers = {"content-type": "text/plain"}
    mock.status_code = 200
    mock.encoding = "utf-8"
    mock.iter_content.side_effect = lambda chunk_size=None: [b"Hello, world!"]
    mock.__enter__.return_value = mock
    with patch("requests.get", return_value=mock):
        result = fn(url="https://example.com/hello.txt")
    assert not result.startswith("Error:"), f"Text response must not be rejected as binary: {result!r}"
    assert "Hello, world!" in result
