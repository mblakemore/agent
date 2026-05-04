import os
import unittest
import requests
import pytest
from unittest.mock import patch, MagicMock
from tools.web_fetch import fn

@pytest.fixture
def mock_resp():
    mock = MagicMock()
    mock.text = "Hello World"
    mock.url = "http://example.com/text.txt"  # needed so the redirect guard passes
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
        assert "Error: fetching URL" in result

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
    mock.url = "http://example.com/test"
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
    mock.url = "https://example.com/photo.jpg"
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
    mock.url = "https://example.com/data.bin"
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
    mock.url = "https://example.com/hello.txt"
    mock.headers = {"content-type": "text/plain"}
    mock.status_code = 200
    mock.encoding = "utf-8"
    mock.iter_content.side_effect = lambda chunk_size=None: [b"Hello, world!"]
    mock.__enter__.return_value = mock
    with patch("requests.get", return_value=mock):
        result = fn(url="https://example.com/hello.txt")
    assert not result.startswith("Error:"), f"Text response must not be rejected as binary: {result!r}"
    assert "Hello, world!" in result


# ── Null byte in URL (#797) ───────────────────────────────────────────────────

def test_web_fetch_null_byte_in_url_rejected_before_network():
    """A null byte anywhere in the URL must be rejected immediately — before any
    network call — with a clear error message (#797).

    Without this guard the null byte would be percent-encoded by the requests
    library and a DNS lookup would be attempted, making a potentially slow
    network round-trip for an obviously invalid URL.
    """
    with patch("requests.get") as mock_get:
        result = fn(url="http://example.com\x00/path")
    assert isinstance(result, str), "fn() must return a string, not raise"
    assert result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}"
    assert "null" in result.lower(), f"Error must mention null byte: {result!r}"
    mock_get.assert_not_called()


def test_web_fetch_null_byte_in_hostname_rejected_before_network():
    """A null byte in the hostname portion of an http URL must also be caught (#797)."""
    with patch("requests.get") as mock_get:
        result = fn(url="http://\x00evil.com/")
    assert isinstance(result, str)
    assert result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}"
    assert "null" in result.lower(), f"Error must mention null byte: {result!r}"
    mock_get.assert_not_called()


def test_web_fetch_null_byte_in_https_url_rejected_before_network():
    """Same guard applies to https:// URLs (#797)."""
    with patch("requests.get") as mock_get:
        result = fn(url="https://example.com/page\x00.html")
    assert isinstance(result, str)
    assert result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}"
    assert "null" in result.lower(), f"Error must mention null byte: {result!r}"
    mock_get.assert_not_called()


def test_web_fetch_request_exception_error_format():
    """RequestException must produce 'Error: fetching URL: ...' (not 'Error fetching URL: ...')."""
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        result = fn("http://example.com/test")
    assert isinstance(result, str)
    assert result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}"
    assert "fetching URL" in result, f"Expected 'fetching URL' in message, got: {result!r}"


# ── Redirect guard / SSRF prevention (#812) ──────────────────────────────────

def _make_mock_response(final_url, content=b"hello world", content_type="text/plain", status_code=200):
    mock_resp = MagicMock()
    mock_resp.url = final_url
    mock_resp.raise_for_status = MagicMock()  # no-op
    mock_resp.headers = {"content-type": content_type}
    mock_resp.encoding = "utf-8"
    mock_resp.iter_content = MagicMock(return_value=iter([content]))
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestWebFetchRedirectGuard(unittest.TestCase):
    """Redirect-based SSRF: the final URL after redirects must be validated (#812)."""

    def test_redirect_to_localhost_blocked(self):
        """A redirect to localhost must be blocked with an error."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://localhost:8080/admin")
            result = fn("http://example.com/redirect-me")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertTrue(
            "private" in result.lower() or "internal" in result.lower(),
            f"Error must mention 'private' or 'internal': {result!r}",
        )

    def test_redirect_to_link_local_blocked(self):
        """A redirect to a link-local (AWS IMDSv1) address must be blocked."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://169.254.169.254/latest/meta-data/")
            result = fn("http://example.com/redirect-me")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertTrue(
            "private" in result.lower() or "internal" in result.lower(),
            f"Error must mention 'private' or 'internal': {result!r}",
        )

    def test_redirect_to_rfc1918_blocked(self):
        """A redirect to an RFC 1918 private address must be blocked."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://10.0.0.1/")
            result = fn("http://example.com/redirect-me")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertTrue(
            "private" in result.lower() or "internal" in result.lower(),
            f"Error must mention 'private' or 'internal': {result!r}",
        )

    def test_no_redirect_passes_through(self):
        """When the final URL is the same as the input (no redirect), the request must succeed."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("https://example.com/page")
            result = fn("https://example.com/page")
        self.assertFalse(
            result.startswith("Error:"),
            f"Non-redirected external URL must not return an error: {result!r}",
        )

    def test_redirect_to_external_passes_through(self):
        """A redirect to a different external site must be allowed through."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("https://other-external-site.com/")
            result = fn("http://example.com/redirect-me")
        self.assertFalse(
            result.startswith("Error:"),
            f"Redirect to external site must not return an error: {result!r}",
        )
