"""Read PDF tool — extract text from PDF files with page range support.

NOTE: This tool is for PDF files ONLY. Do NOT use it to read .log, .py, .json, or other text files.
Use the file tool (action='read') for text files.
"""

import os
from pathlib import Path

import fitz  # PyMuPDF


_MAX_PAGES_PER_CALL = 50


def fn(path: str, start_page: int = 1, end_page: int = 0) -> str:
    """Extract text from a PDF file.

    Args:
        path: Path to the PDF file.
        start_page: First page to extract (1-indexed, default: 1).
        end_page: Last page to extract (1-indexed, inclusive). 0 = last page.
    """
    if not isinstance(path, str):
        return f"Error: 'path' must be a string, got {type(path).__name__}"
    if '\x00' in path:
        return "Error: path contains a null byte, which is not allowed"
    # Confinement: reject paths that resolve outside the working directory (#872)
    try:
        cwd_resolved = Path.cwd().resolve()
        cwd_prefix = str(cwd_resolved) + os.sep
        resolved = Path(path).resolve()
        if resolved != cwd_resolved and not str(resolved).startswith(cwd_prefix):
            return (
                f"Error: path '{path}' resolves to '{resolved}' which is outside "
                f"the working directory '{cwd_resolved}'. "
                f"Only files inside the current working directory can be accessed."
            )
    except (OSError, ValueError):
        pass  # let fitz.open fail naturally
    try:
        doc = fitz.open(path)
    except Exception as e:
        return f"Error: opening PDF: {e}"

    if not doc.is_pdf:
        doc.close()
        return (
            f"Error: '{path}' is not a PDF file. "
            "Use the 'file' tool with action='read' for text files."
        )

    # fitz.open() succeeds on encrypted PDFs but leaves the document in a
    # locked state (needs_pass=1).  Accessing pages then raises
    # ValueError: document closed or encrypted.  Detect this early and
    # return a clear error rather than letting the page loop crash.
    if doc.needs_pass:
        doc.close()
        return (
            f"Error: '{path}' is encrypted/password-protected. "
            "A password is required to read this file."
        )

    total = len(doc)
    if total == 0:
        doc.close()
        return "Error: PDF has no pages"

    # Booleans are a subclass of int in Python; True==1 and False==0, so
    # start_page=True would silently read page 1 and start_page=False would
    # trigger a confusing "< 1" error rather than a clear type error.
    # Reject them explicitly, consistent with the file tool's line-number guards.
    if isinstance(start_page, bool):
        doc.close()
        return (
            f"Error: start_page must be a plain integer, got bool ({start_page!r}). "
            "Pass a plain integer page number (e.g. start_page=1)."
        )
    if isinstance(end_page, bool):
        doc.close()
        return (
            f"Error: end_page must be a plain integer, got bool ({end_page!r}). "
            "Pass a plain integer page number, or 0 for last page."
        )

    # Validate start_page type before numeric comparisons.
    # Float page numbers with a fractional part (e.g. 1.5, 6.9) must be rejected
    # rather than silently truncated — int(1.5) == 1, which would read the wrong page.
    # Whole-number floats (e.g. 2.0) are safe to coerce, consistent with task_tracker.
    if not isinstance(start_page, int):
        try:
            coerced = int(start_page)
            if isinstance(start_page, float) and start_page != coerced:
                doc.close()
                return (
                    f"Error: start_page must be an integer, got non-integer float: {start_page!r}. "
                    f"Did you mean {coerced} or {coerced + 1}?"
                )
            start_page = coerced
        except (TypeError, ValueError):
            doc.close()
            return f"Error: start_page must be an integer, got {type(start_page).__name__!r}"

    # Validate end_page type before numeric comparisons.
    # Same fractional-float guard as start_page.
    if not isinstance(end_page, int):
        try:
            coerced = int(end_page)
            if isinstance(end_page, float) and end_page != coerced:
                doc.close()
                return (
                    f"Error: end_page must be an integer, got non-integer float: {end_page!r}. "
                    f"Did you mean {coerced} or {coerced + 1}?"
                )
            end_page = coerced
        except (TypeError, ValueError):
            doc.close()
            return f"Error: end_page must be an integer, got {type(end_page).__name__!r}"

    # Validate start_page — must be 1-indexed (≥ 1)
    if start_page < 1:
        doc.close()
        return (
            f"Error: start_page ({start_page}) is invalid — "
            "pages are 1-indexed (minimum value: 1)"
        )

    # Validate end_page — must be 0 (last page sentinel) or a valid 1-indexed page number
    if end_page < 0:
        doc.close()
        return (
            f"Error: end_page ({end_page}) is invalid — "
            "use 0 to mean last page, or a positive page number (1-indexed)"
        )
    if end_page > total:
        doc.close()
        return f"Error: end_page ({end_page}) exceeds page count ({total})"

    # Resolve page range
    start = start_page
    end = end_page if end_page > 0 else total

    if start > total:
        doc.close()
        return f"Error: start_page ({start}) exceeds page count ({total})"

    if end_page > 0 and end < start:
        doc.close()
        return f"Error: end_page ({end_page}) is less than start_page ({start_page})"

    # Cap to avoid flooding context
    if end - start + 1 > _MAX_PAGES_PER_CALL:
        end = start + _MAX_PAGES_PER_CALL - 1

    parts = [f"[PDF: {path} | Pages {start}-{end} of {total}]"]
    if end < total:
        parts.append(f"[Use read_pdf with start_page={end + 1} to continue reading]")

    try:
        for page_num in range(start - 1, end):
            page = doc[page_num]
            text = page.get_text().strip()
            parts.append(f"\n--- Page {page_num + 1} ---\n{text}")
    except Exception as e:
        doc.close()
        return f"Error: reading PDF page: {e}"

    doc.close()
    return "\n".join(parts)


definition = {
    "type": "function",
    "function": {
        "name": "read_pdf",
        "description": (
            "Extract text from a PDF file (*.pdf only — do NOT use for .py, .md, .json, or other text files; use the 'file' tool with action='read' for those). "
            "Supports page ranges for large documents. "
            "Returns up to 50 pages per call — use start_page to paginate through "
            "longer PDFs. Use this for reading books, papers, and documents for learning."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the PDF file.",
                },
                "start_page": {
                    "type": "integer",
                    "description": "First page to extract (1-indexed, default: 1).",
                    "default": 1,
                },
                "end_page": {
                    "type": "integer",
                    "description": "Last page to extract (1-indexed, inclusive). 0 = last page.",
                    "default": 0,
                },
            },
            "required": ["path"],
        },
    },
}
