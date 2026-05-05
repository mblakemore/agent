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

    def test_redirect_to_172_17_blocked(self):
        """A redirect to 172.17.x.x must be blocked — within RFC 1918 172.16.0.0/12 range (#815)."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://172.17.0.1/")
            result = fn("http://example.com/redirect-me")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertTrue(
            "private" in result.lower() or "internal" in result.lower(),
            f"Error must mention 'private' or 'internal': {result!r}",
        )

    def test_redirect_to_172_31_blocked(self):
        """A redirect to 172.31.x.x must be blocked — top of RFC 1918 172.16.0.0/12 range (#815)."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://172.31.255.254/")
            result = fn("http://example.com/redirect-me")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertTrue(
            "private" in result.lower() or "internal" in result.lower(),
            f"Error must mention 'private' or 'internal': {result!r}",
        )

    def test_redirect_to_172_16_still_blocked(self):
        """A redirect to 172.16.x.x must still be blocked after the ipaddress refactor (#815)."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://172.16.0.1/")
            result = fn("http://example.com/redirect-me")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertTrue(
            "private" in result.lower() or "internal" in result.lower(),
            f"Error must mention 'private' or 'internal': {result!r}",
        )

    def test_redirect_to_172_32_passes_through(self):
        """A redirect to 172.32.x.x must be allowed — outside RFC 1918 range (#815)."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://172.32.0.1/")
            result = fn("http://example.com/redirect-me")
        self.assertFalse(
            result.startswith("Error:"),
            f"172.32.x.x is public and must not be blocked: {result!r}",
        )


class TestIsPrivateAddress(unittest.TestCase):
    """Unit tests for the _is_private_address helper (#815)."""

    def setUp(self):
        from tools.web_fetch import _is_private_address
        self.check = _is_private_address

    def test_loopback_ipv4(self):
        self.assertTrue(self.check("http://127.0.0.1/"))
        self.assertTrue(self.check("http://127.255.255.255/"))

    def test_rfc1918_10(self):
        self.assertTrue(self.check("http://10.0.0.1/"))
        self.assertTrue(self.check("http://10.255.255.255/"))

    def test_rfc1918_172_full_range(self):
        for last in (16, 17, 20, 24, 31):
            url = f"http://172.{last}.0.1/"
            self.assertTrue(self.check(url), f"Expected private: {url}")

    def test_rfc1918_172_outside_range(self):
        self.assertFalse(self.check("http://172.15.0.1/"))
        self.assertFalse(self.check("http://172.32.0.1/"))

    def test_rfc1918_192_168(self):
        self.assertTrue(self.check("http://192.168.0.1/"))

    def test_link_local(self):
        self.assertTrue(self.check("http://169.254.169.254/"))

    def test_ipv6_loopback(self):
        self.assertTrue(self.check("http://[::1]/"))

    def test_ipv6_ula_fc(self):
        self.assertTrue(self.check("http://[fc00::1]/"))

    def test_ipv6_ula_fd(self):
        self.assertTrue(self.check("http://[fd00::1]/"))

    def test_public_ip(self):
        self.assertFalse(self.check("http://8.8.8.8/"))
        self.assertFalse(self.check("https://93.184.216.34/"))

    def test_hostname_passes(self):
        # Hostnames are not numeric IPs and must not be flagged
        self.assertFalse(self.check("http://example.com/"))
        self.assertFalse(self.check("http://internal-service/"))

    def test_empty_host(self):
        self.assertFalse(self.check("http:///path"))

    def test_localhost_hostname(self):
        # 'localhost' is explicitly blocked as it always resolves to loopback
        self.assertTrue(self.check("http://localhost/"))

    def test_ipv4_mapped_loopback(self):
        """::ffff:127.0.0.1 must be treated as loopback, not a public IPv6 address. (#874)"""
        self.assertTrue(self.check("http://[::ffff:127.0.0.1]/"))
        self.assertTrue(self.check("http://[::ffff:7f00:1]/"))

    def test_ipv4_mapped_rfc1918(self):
        """::ffff:192.168.x.x must be treated as RFC-1918 private. (#874)"""
        self.assertTrue(self.check("http://[::ffff:192.168.1.1]/"))
        self.assertTrue(self.check("http://[::ffff:10.0.0.1]/"))

    def test_ipv4_mapped_link_local(self):
        """::ffff:169.254.169.254 must be treated as link-local (AWS IMDS). (#874)"""
        self.assertTrue(self.check("http://[::ffff:169.254.169.254]/"))

    def test_ipv4_mapped_public(self):
        """::ffff:8.8.8.8 must be allowed — it maps to a public IPv4 address. (#874)"""
        self.assertFalse(self.check("http://[::ffff:8.8.8.8]/"))


# ── Credential scrubbing (#822) ───────────────────────────────────────────────


class TestStripCredentials(unittest.TestCase):
    """Unit tests for _strip_credentials — credentials must be removed from URLs before
    being written to disk or returned in tool output."""

    def setUp(self):
        from tools.web_fetch import _strip_credentials
        self.strip = _strip_credentials

    def test_user_and_password_removed(self):
        result = self.strip("http://user:password@example.com/")
        assert "user" not in result, f"Username still present: {result!r}"
        assert "password" not in result, f"Password still present: {result!r}"
        assert "example.com" in result, f"Host was lost: {result!r}"

    def test_token_only_removed(self):
        """A bare token (no colon) in the netloc must also be removed."""
        result = self.strip("https://token@example.com/page")
        assert "token" not in result, f"Token still present: {result!r}"
        assert "example.com" in result

    def test_port_retained(self):
        """The port number must be kept after stripping credentials."""
        result = self.strip("https://user:pass@example.com:8443/api")
        assert ":8443" in result, f"Port was lost: {result!r}"
        assert "user" not in result
        assert "pass" not in result

    def test_path_retained(self):
        """The path component must survive credential stripping."""
        result = self.strip("http://user:s3cr3t@example.com/some/path?q=1")
        assert "/some/path" in result, f"Path was lost: {result!r}"

    def test_no_credentials_unchanged(self):
        """A URL with no credentials must be returned unchanged."""
        url = "https://example.com/page"
        result = self.strip(url)
        assert result == url, f"URL without credentials was modified: {result!r}"

    def test_scheme_retained(self):
        """The scheme (http/https) must be preserved."""
        result = self.strip("https://u:p@example.com/")
        assert result.startswith("https://"), f"Scheme changed: {result!r}"


def _make_credentialed_mock_response(final_url, content=b"secret content"):
    mock_resp = MagicMock()
    mock_resp.url = final_url
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "text/plain"}
    mock_resp.encoding = "utf-8"
    mock_resp.iter_content = MagicMock(return_value=iter([content]))
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestWebFetchCredentialScrubbing(unittest.TestCase):
    """Integration tests: credentials embedded in the URL must NOT appear in the
    tool result string or in the saved .md file (#XXX)."""

    def test_credentials_not_in_tool_result(self):
        """The tool result string must not contain the password."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_credentialed_mock_response("http://example.com/")
            result = fn("http://user:s3cr3t@example.com/")
        assert "s3cr3t" not in result, (
            f"Password appeared in tool result: {result!r}"
        )
        assert "user:s3cr3t" not in result

    def test_username_not_in_tool_result(self):
        """The username must also be absent from the tool result."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_credentialed_mock_response("http://example.com/")
            result = fn("http://myuser:mypass@example.com/")
        assert "myuser" not in result, (
            f"Username appeared in tool result: {result!r}"
        )

    def test_credentials_not_in_saved_file(self, tmp_path=None):
        """The saved .md file header must not contain the embedded credentials."""
        import tempfile, os
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_credentialed_mock_response("https://example.com/page")
            with patch("tools.web_fetch.os.getcwd", return_value=tempfile.mkdtemp()):
                result = fn("https://admin:hunter2@example.com/page")
        # Extract save path from the result
        import re
        m = re.search(r'saved to (\S+\.md)', result)
        assert m is not None, f"Could not find save path in result: {result!r}"
        save_path = m.group(1)
        if os.path.exists(save_path):
            saved = open(save_path, encoding="utf-8").read()
            assert "hunter2" not in saved, (
                f"Password appeared in saved file header: {saved[:200]!r}"
            )
            assert "admin:hunter2" not in saved  # #822

    def test_host_present_in_tool_result(self):
        """The hostname must still appear in the tool result after credential scrubbing."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_credentialed_mock_response("https://example.com/")
            result = fn("https://user:pass@example.com/")
        assert "example.com" in result, (
            f"Hostname was lost from tool result: {result!r}"
        )

    def test_redirect_to_private_with_credentials_strips_creds(self):
        """Redirect to private address with embedded credentials must not expose them (#970)."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("http://user:s3cr3t@127.0.0.1/")
            result = fn("http://example.com/redirect")
        assert result.startswith("Error:"), f"Expected error: {result!r}"
        assert "s3cr3t" not in result, f"Credential leaked in redirect error: {result!r}"
        assert "user:s3cr3t" not in result, f"Credential pair leaked in redirect error: {result!r}"
        assert "127.0.0.1" in result, f"Host address must still appear in error: {result!r}"

    def test_redirect_non_http_with_credentials_strips_creds(self):
        """Redirect to non-HTTP URL with embedded credentials must not expose them (#970)."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response("ftp://admin:p4ssw0rd@internal.host/file")
            result = fn("http://example.com/redirect")
        assert result.startswith("Error:"), f"Expected error: {result!r}"
        assert "p4ssw0rd" not in result, f"Credential leaked in non-HTTP redirect error: {result!r}"
        assert "admin:p4ssw0rd" not in result, f"Credential pair leaked: {result!r}"


# ── Malformed Content-Length header (#831) ────────────────────────────────────


def _make_mock_response_with_content_length(content_length_value, content=b"valid page content"):
    """Build a mock response with a custom Content-Length header value."""
    mock_resp = MagicMock()
    mock_resp.url = "http://example.com/page"
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "text/plain", "content-length": content_length_value}
    mock_resp.encoding = "utf-8"
    mock_resp.iter_content = MagicMock(return_value=iter([content]))
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestWebFetchMalformedContentLength(unittest.TestCase):
    """A non-numeric Content-Length header must not crash web_fetch (#831)."""

    def test_non_numeric_content_length_still_fetches_page(self):
        """A server returning Content-Length: 'not-a-number' must not fail the fetch.

        Without the fix, int('not-a-number') raises ValueError which propagates
        to the outer except-Exception handler and returns a cryptic error.
        With the fix, the bad header is skipped and streaming proceeds normally.
        """
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response_with_content_length("not-a-number")
            result = fn("http://example.com/page")
        self.assertFalse(
            result.startswith("Error:"),
            f"Malformed Content-Length caused fetch failure: {result!r}",
        )
        self.assertIn("valid page content", result)

    def test_chunked_content_length_still_fetches_page(self):
        """'0, chunked' is a real-world malformed Content-Length value that must be tolerated."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response_with_content_length("0, chunked")
            result = fn("http://example.com/page")
        self.assertFalse(
            result.startswith("Error:"),
            f"'0, chunked' Content-Length caused fetch failure: {result!r}",
        )

    def test_valid_numeric_content_length_still_enforced(self):
        """A valid numeric Content-Length above the limit must still be rejected."""
        over_limit = str(1024 * 1024 + 1)  # 1 byte over _MAX_BYTES
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response_with_content_length(over_limit)
            result = fn("http://example.com/page")
        self.assertTrue(
            result.startswith("Error:"),
            f"Oversized Content-Length was not rejected: {result!r}",
        )
        self.assertIn("too large", result)

    def test_valid_small_content_length_passes(self):
        """A valid numeric Content-Length within the limit must allow the fetch."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response_with_content_length("100")
            result = fn("http://example.com/page")
        self.assertFalse(
            result.startswith("Error:"),
            f"Valid Content-Length caused fetch failure: {result!r}",
        )

    def test_missing_content_length_header_still_fetches(self):
        """Absence of Content-Length header must not affect the fetch."""
        mock_resp = MagicMock()
        mock_resp.url = "http://example.com/page"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"content-type": "text/plain"}  # no content-length
        mock_resp.encoding = "utf-8"
        mock_resp.iter_content = MagicMock(return_value=iter([b"valid page content"]))
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("tools.web_fetch.requests.get", return_value=mock_resp):
            result = fn("http://example.com/page")
        self.assertFalse(
            result.startswith("Error:"),
            f"Missing Content-Length caused fetch failure: {result!r}",
        )


# ── Pre-request SSRF guard (#849) ────────────────────────────────────────────
# _is_private_address was only called on final_url (post-redirect), meaning a
# direct request to a private IP was fully fetched before the block fired.
# The fix adds a pre-request check so no network I/O occurs for private URLs.


class TestWebFetchPreRequestSSRFGuard(unittest.TestCase):
    """Direct requests to private/internal addresses must be blocked before any
    network I/O occurs (#849)."""

    def _assert_blocked_before_network(self, url):
        """Helper: assert fn() returns an error and requests.get is never called."""
        with patch("tools.web_fetch.requests.get") as mock_get:
            result = fn(url)
        self.assertTrue(
            result.startswith("Error:"),
            f"Expected 'Error:' prefix for private URL {url!r}: {result!r}",
        )
        mock_get.assert_not_called()

    def test_loopback_direct_blocked_before_network(self):
        """Direct request to http://127.0.0.1/ must be rejected without a network call."""
        self._assert_blocked_before_network("http://127.0.0.1/secret")

    def test_localhost_direct_blocked_before_network(self):
        """Direct request to http://localhost/ must be rejected without a network call."""
        self._assert_blocked_before_network("http://localhost/admin")

    def test_rfc1918_10_direct_blocked_before_network(self):
        """Direct request to an RFC 1918 10.x.x.x address must be blocked before network."""
        self._assert_blocked_before_network("http://10.0.0.1/internal")

    def test_rfc1918_192_168_direct_blocked_before_network(self):
        """Direct request to a 192.168.x.x address must be blocked before network."""
        self._assert_blocked_before_network("http://192.168.1.1/router")

    def test_link_local_aws_imds_direct_blocked_before_network(self):
        """Direct request to the AWS IMDSv1 address must be blocked before network."""
        self._assert_blocked_before_network("http://169.254.169.254/latest/meta-data/")

    def test_ipv6_loopback_direct_blocked_before_network(self):
        """Direct request to the IPv6 loopback address must be blocked before network."""
        self._assert_blocked_before_network("http://[::1]/secret")

    def test_error_message_contains_private_or_internal(self):
        """Error message for a blocked private URL must say 'private' or 'internal'."""
        with patch("tools.web_fetch.requests.get"):
            result = fn("http://127.0.0.1/secret")
        self.assertTrue(
            "private" in result.lower() or "internal" in result.lower(),
            f"Error must mention 'private' or 'internal': {result!r}",
        )

    def test_public_ip_not_blocked_by_pre_check(self):
        """A public IP must still reach the network (pre-check must not over-block)."""
        mock_resp = MagicMock()
        mock_resp.url = "http://8.8.8.8/"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.encoding = "utf-8"
        mock_resp.iter_content = MagicMock(return_value=iter([b"ok"]))
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("tools.web_fetch.requests.get", return_value=mock_resp) as mock_get:
            fn("http://8.8.8.8/")
        mock_get.assert_called_once()

    def test_credentials_stripped_from_pre_check_error(self):
        """Error message for a credentialed private URL must not expose the password."""
        with patch("tools.web_fetch.requests.get"):
            result = fn("http://user:s3cr3t@127.0.0.1/secret")
        self.assertNotIn("s3cr3t", result, f"Password leaked in error: {result!r}")
        self.assertTrue(result.startswith("Error:"), f"Expected error, got: {result!r}")

    def test_hex_ip_loopback_blocked_before_network(self):
        """Hex IP 0x7f000001 (= 127.0.0.1) must be blocked before any network call (#876)."""
        self._assert_blocked_before_network("http://0x7f000001/secret")

    def test_octal_ip_loopback_blocked_before_network(self):
        """Octal IP 0177.0.0.1 (= 127.0.0.1) must be blocked before any network call (#876)."""
        self._assert_blocked_before_network("http://0177.0.0.1/secret")

    def test_decimal_integer_ip_loopback_blocked_before_network(self):
        """Decimal integer IP 2130706433 (= 127.0.0.1) must be blocked before any network call (#876)."""
        self._assert_blocked_before_network("http://2130706433/secret")


class TestIsPrivateAddressNumericFormats(unittest.TestCase):
    """Unit tests for non-standard numeric IP SSRF bypass (#876).

    ipaddress.ip_address() raises ValueError for hex/octal/integer forms, but
    the OS socket layer resolves them to private addresses.  _is_private_address
    must detect and block these formats.
    """

    def setUp(self):
        from tools.web_fetch import _is_private_address
        self.check = _is_private_address

    def test_hex_loopback_blocked(self):
        """0x7f000001 resolves to 127.0.0.1 — must be flagged as private."""
        self.assertTrue(self.check("http://0x7f000001/"), "hex 0x7f000001 must be private")

    def test_octal_loopback_blocked(self):
        """0177.0.0.1 resolves to 127.0.0.1 — must be flagged as private."""
        self.assertTrue(self.check("http://0177.0.0.1/"), "octal 0177.0.0.1 must be private")

    def test_decimal_integer_loopback_blocked(self):
        """2130706433 resolves to 127.0.0.1 — must be flagged as private."""
        self.assertTrue(self.check("http://2130706433/"), "decimal 2130706433 must be private")

    def test_hex_public_ip_allowed(self):
        """0x08080808 resolves to 8.8.8.8 — must be allowed through."""
        self.assertFalse(self.check("http://0x08080808/"), "hex 0x08080808 (8.8.8.8) must not be private")


# ── URL type validation (#893) ────────────────────────────────────────────────


class TestWebFetchUrlTypeValidation(unittest.TestCase):
    """Non-string url values must return a clear type error, not 'must not be empty' (#893).

    Before the fix, the guard was:
        if not isinstance(url, str) or not url.strip():
            return "Error: url must not be empty"
    so passing an integer, None, or a list would produce a misleading
    'must not be empty' error.  The fix splits the check into two guards.
    """

    def test_integer_url_returns_type_error(self):
        """fn(42) must return a 'must be a string' error, not 'must not be empty' (#893)."""
        result = fn(42)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertNotIn("empty", result, f"Error must NOT say 'empty': {result!r}")
        self.assertIn("int", result, f"Error must name the bad type: {result!r}")

    def test_none_url_returns_type_error(self):
        """fn(None) must return a 'must be a string' error, not 'must not be empty' (#893)."""
        result = fn(None)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertNotIn("empty", result, f"Error must NOT say 'empty': {result!r}")
        self.assertIn("NoneType", result, f"Error must name the bad type: {result!r}")

    def test_list_url_returns_type_error(self):
        """fn([...]) must return a 'must be a string' error (#893)."""
        result = fn(["http://example.com"])
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("list", result, f"Error must name the bad type: {result!r}")

    def test_float_url_returns_type_error(self):
        """fn(3.14) must return a 'must be a string' error (#893)."""
        result = fn(3.14)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("string", result, f"Error must mention 'string': {result!r}")
        self.assertIn("float", result, f"Error must name the bad type: {result!r}")

    def test_non_string_url_does_not_reach_network(self):
        """Non-string url must be rejected before any network call (#893)."""
        with patch("requests.get") as mock_get:
            fn(42)
        mock_get.assert_not_called()

    def test_empty_string_url_still_returns_empty_error(self):
        """An actual empty string must still return the 'must not be empty' message (#893)."""
        result = fn("")
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("empty", result, f"Empty string should say 'empty': {result!r}")

    def test_valid_string_url_unaffected(self):
        """A valid URL string must still be accepted normally (#893)."""
        mock_resp = MagicMock()
        mock_resp.url = "http://example.com/"
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.status_code = 200
        mock_resp.encoding = "utf-8"
        mock_resp.iter_content.return_value = iter([b"hello"])
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("requests.get", return_value=mock_resp):
            result = fn("http://example.com/")
        self.assertFalse(result.startswith("Error:"), f"Valid URL should not return error: {result!r}")
