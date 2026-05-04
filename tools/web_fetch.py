"""Web fetch tool — retrieve and convert web pages to readable text.

Content is saved to a file and a short summary is returned to keep the
conversation context small. The agent can then read the file with the
file tool if it needs the full content.
"""

import hashlib
import os
import requests
from markdownify import markdownify


_MAX_CHARS = 50000
_MAX_BYTES = 1024 * 1024  # 1MB limit to prevent OOM
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; agent/1.0)"
}
# Max chars to include inline in the tool result (keeps context small)
_INLINE_PREVIEW = 2000

# Prefixes blocked after redirect resolution — prevents SSRF via open redirect
# to internal/private addresses even when the original URL was external.
_BLOCKED_URL_PREFIXES = (
    "http://localhost", "https://localhost",
    "http://127.", "https://127.",
    "http://169.254.", "https://169.254.",    # link-local / AWS IMDSv1
    "http://10.", "https://10.",               # RFC 1918
    "http://172.16.", "https://172.16.",       # RFC 1918 (partial — covers 172.16.0.0/12 first octet only)
    "http://192.168.", "https://192.168.",     # RFC 1918
    "http://0.", "https://0.",                 # 0.0.0.0/8
    "http://[::1]", "https://[::1]",           # IPv6 loopback
    "http://[fc", "https://[fc",               # IPv6 ULA
    "http://[fd", "https://[fd",               # IPv6 ULA
)


def fn(url: str) -> str:
    """Fetch a URL and save its content to a file.

    Returns a short preview + the file path.  Use the file tool to read
    the full content.

    Args:
        url: The URL to fetch.
    """
    if not isinstance(url, str) or not url.strip():
        return "Error: url must not be empty"
    if '\x00' in url:
        return "Error: url contains a null byte, which is not allowed"
    if not url.startswith(("http://", "https://")):
        return f"Error: invalid URL '{url}' — must begin with http:// or https://"
    try:
        with requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()

            # Validate the final URL after redirects — a server could redirect to an
            # internal address even though the original URL was external.
            final_url = resp.url
            if not final_url.startswith(("http://", "https://")):
                return f"Error: redirect led to non-HTTP URL '{final_url}'"
            if final_url.lower().startswith(tuple(p.lower() for p in _BLOCKED_URL_PREFIXES)):
                return f"Error: redirect to private/internal address is not allowed: '{final_url}'"

            content_type = resp.headers.get("content-type", "").lower()
            
            # 1. Fast-fail if Content-Length is obviously too large
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > _MAX_BYTES:
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

    # Save to file so the full content survives context compression
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    save_dir = os.path.join(os.getcwd(), ".agent", "state", "fetched")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, f"{url_hash}.md")
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(f"# Fetched: {url}\n\n{text}")

    total_chars = len(text)
    total_lines = text.count("\n") + 1
    preview = text[:_INLINE_PREVIEW]
    if len(text) > _INLINE_PREVIEW:
        preview += f"\n\n[... truncated — {total_chars} chars total, {total_lines} lines]"

    return (
        f"[Fetched: {url} — saved to {save_path} ({total_chars} chars, {total_lines} lines)]\n"
        f"[Use file tool to read full content if needed]\n\n"
        f"{preview}"
    )


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
