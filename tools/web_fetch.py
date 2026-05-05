"""Web fetch tool — retrieve and convert web pages to readable text.

Content is saved to a file and a short summary is returned to keep the
conversation context small. The agent can then read the file with the
file tool if it needs the full content.
"""

import hashlib
import ipaddress
import os
import re
import socket
import requests
from markdownify import markdownify
from urllib.parse import urlparse, urlunparse


_MAX_CHARS = 50000
_MAX_BYTES = 1024 * 1024  # 1MB limit to prevent OOM
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; agent/1.0)"
}
# Max chars to include inline in the tool result (keeps context small)
_INLINE_PREVIEW = 2000

# Private/reserved networks — used to block SSRF redirects to internal addresses.
# This covers all RFC 1918 ranges, loopback, link-local, and IPv6 private space
# using the ipaddress module for accurate subnet matching (e.g. the full
# 172.16.0.0/12 range, not just 172.16.x.x).
_PRIVATE_NETWORKS = [
    ipaddress.ip_network(cidr) for cidr in (
        "127.0.0.0/8",      # loopback
        "10.0.0.0/8",       # RFC 1918
        "172.16.0.0/12",    # RFC 1918 — covers 172.16.x.x through 172.31.x.x
        "192.168.0.0/16",   # RFC 1918
        "169.254.0.0/16",   # link-local / AWS IMDSv1
        "0.0.0.0/8",        # reserved
        "::1/128",          # IPv6 loopback
        "fc00::/7",         # IPv6 ULA (fc00:: and fd00::)
    )
]

# Matches non-standard numeric IP formats that ipaddress rejects but the OS
# socket layer resolves: hex (0x7f000001), decimal integer (2130706433),
# octal per-octet (0177.0.0.1).  Used to detect SSRF bypass attempts (#876).
_NUMERIC_HOST_RE = re.compile(r'^(?:0[xX][0-9a-fA-F]+|[0-9]+(?:\.[0-9]+){0,3})$')


def _addr_is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if addr (after unwrapping any IPv4-mapped IPv6) is private."""
    if hasattr(addr, 'ipv4_mapped') and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _PRIVATE_NETWORKS)


def _strip_credentials(url: str) -> str:
    """Return the URL with any embedded user:password credentials removed.

    ``http://user:pass@host/path`` → ``http://host/path``
    ``https://token@host/path``  → ``https://host/path``
    URLs without credentials are returned unchanged.
    """
    try:
        parsed = urlparse(url)
        # Build a netloc that contains only host[:port], no userinfo.
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        clean = urlunparse(parsed._replace(netloc=host))
        return clean
    except Exception:
        return url


def _is_private_address(url: str) -> bool:
    """Return True if the URL's host is a private/reserved IP or the loopback hostname.

    Numeric IP addresses are checked against all private/reserved CIDR ranges.
    The 'localhost' hostname is also blocked since it always resolves to loopback.
    Other hostnames are allowed through — DNS resolution happens server-side and
    we cannot resolve arbitrary names here.
    """
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        # Block localhost by name (always resolves to loopback)
        if host.lower() == "localhost":
            return True
        # Strip brackets for IPv6 literals (e.g. "[::1]" → "::1")
        host = host.strip("[]")
        addr = ipaddress.ip_address(host)
        return _addr_is_private(addr)
    except ValueError:
        # Non-standard numeric IP formats (hex 0x7f000001, octal 0177.0.0.1, decimal
        # integer 2130706433) are rejected by ipaddress but resolved by the OS socket
        # layer to private addresses.  Detect them with a pattern match and resolve.
        if _NUMERIC_HOST_RE.match(host):
            try:
                for _, _, _, _, sockaddr in socket.getaddrinfo(host, None):
                    try:
                        resolved = ipaddress.ip_address(sockaddr[0])
                        if _addr_is_private(resolved):
                            return True
                    except ValueError:
                        pass
            except (socket.gaierror, OSError):
                pass
        # Not a numeric IP address or localhost — allow (could be an external hostname)
        return False


def fn(url: str) -> str:
    """Fetch a URL and save its content to a file.

    Returns a short preview + the file path.  Use the file tool to read
    the full content.

    Args:
        url: The URL to fetch.
    """
    if not isinstance(url, str):
        return f"Error: url must be a string, got {type(url).__name__!r}"
    if not url.strip():
        return "Error: url must not be empty"
    if '\x00' in url:
        return "Error: url contains a null byte, which is not allowed"
    if not url.startswith(("http://", "https://")):
        return f"Error: invalid URL '{url}' — must begin with http:// or https://"
    # Pre-request SSRF guard: block direct requests to private/internal addresses
    # before any network I/O occurs.  The post-redirect check inside the response
    # context manager handles redirect-based SSRF, but without this guard a direct
    # request to e.g. http://127.0.0.1/ or http://169.254.169.254/ would be fully
    # fetched (up to 1 MB) before the address was checked and discarded.
    if _is_private_address(url):
        return f"Error: fetching private/internal address is not allowed: '{_strip_credentials(url)}'"
    try:
        with requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()

            # Validate the final URL after redirects — a server could redirect to an
            # internal address even though the original URL was external.
            final_url = resp.url
            safe_final_url = _strip_credentials(final_url)
            if not final_url.startswith(("http://", "https://")):
                return f"Error: redirect led to non-HTTP URL '{safe_final_url}'"
            if _is_private_address(final_url):
                return f"Error: redirect to private/internal address is not allowed: '{safe_final_url}'"

            content_type = resp.headers.get("content-type", "").lower()
            
            # 1. Fast-fail if Content-Length is obviously too large.
            # Guard int() with a try/except: some servers return non-numeric
            # Content-Length values (e.g. "0, chunked", "unk") which would
            # raise ValueError and cause the whole fetch to fail with a
            # cryptic error.  When the header is non-numeric we skip the
            # pre-check and let the streaming byte-count limit do the job.
            content_length = resp.headers.get("content-length")
            try:
                cl_int = int(content_length) if content_length else None
            except (ValueError, TypeError):
                cl_int = None
            if cl_int is not None and cl_int > _MAX_BYTES:
                return f"Error: Remote file is too large ({content_length} bytes). Max allowed is {_MAX_BYTES}."

            # 2. Stream content to avoid OOM
            chunks = []
            bytes_read = 0
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    chunks.append(chunk)
                    bytes_read += len(chunk)
                    if bytes_read > _MAX_BYTES:
                        break
            
            # Join bytes and detect binary content before decoding.
            # Binary responses (images, PDFs, executables, etc.) contain null
            # bytes and are useless as text — return a clear error rather than
            # returning mojibake to the caller.
            full_content = b"".join(chunks)
            if b"\x00" in full_content[:8192]:
                return (
                    f"Error: response body is binary (content-type: {content_type or 'unknown'}). "
                    f"web_fetch only supports text content."
                )
            text = full_content.decode(resp.encoding or 'utf-8', errors='replace')
            
            # Trim to _MAX_CHARS if we exceeded it during streaming
            if len(text) > _MAX_CHARS:
                text = text[:_MAX_CHARS]

            # Handle HTML conversion
            if "text/html" in content_type:
                try:
                    md = markdownify(text, strip=["img", "script", "style", "nav", "footer", "header"])
                    # Clean up excessive blank lines
                    lines = md.splitlines()
                    cleaned = []
                    blank_count = 0
                    for line in lines:
                        if not line.strip():
                            blank_count += 1
                            if blank_count <= 2:
                                cleaned.append("")
                        else:
                            blank_count = 0
                            cleaned.append(line)
                    text = "\n".join(cleaned).strip()
                except Exception:
                    pass  # Fallback to the streamed text
            
            # Final safety trim
            if len(text) > _MAX_CHARS:
                text = text[:_MAX_CHARS]

    except requests.exceptions.RequestException as e:
        return f"Error: fetching URL: {e}"
    except Exception as e:
        return f"Error: {e}"

    # Save to file so the full content survives context compression.
    # Strip any embedded credentials from the URL before writing it to disk
    # or returning it to the caller — credentials must not appear in saved files
    # or tool output.
    display_url = _strip_credentials(url)
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    save_dir = os.path.join(os.getcwd(), ".agent", "state", "fetched")
    save_path = None
    save_error = None
    try:
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{url_hash}.md")
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(f"# Fetched: {display_url}\n\n{text}")
    except Exception as e:
        save_error = str(e)
        save_path = None

    total_chars = len(text)
    total_lines = text.count("\n") + 1
    preview = text[:_INLINE_PREVIEW]
    if len(text) > _INLINE_PREVIEW:
        preview += f"\n\n[... truncated — {total_chars} chars total, {total_lines} lines]"

    if save_path:
        header = (
            f"[Fetched: {display_url} — saved to {save_path} ({total_chars} chars, {total_lines} lines)]\n"
            f"[Use file tool to read full content if needed]\n"
        )
    else:
        header = (
            f"[Fetched: {display_url} ({total_chars} chars, {total_lines} lines)"
            f" — Warning: could not save to disk: {save_error}]\n"
        )
    return header + "\n" + preview


definition = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch a web page, save it to .agent/state/fetched/<hash>.md, and return "
            "a short preview. The full content is in the saved file — use the "
            "file tool to read it. Do NOT re-fetch a URL that was already fetched; "
            "read the saved file instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch.",
                },
            },
            "required": ["url"],
        },
    },
}
