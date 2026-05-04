"""Read PDF tool — extract text from PDF files with page range support.

NOTE: This tool is for PDF files ONLY. Do NOT use it to read .log, .py, .json, or other text files.
Use the file tool (action='read') for text files.
"""

import fitz  # PyMuPDF


_MAX_PAGES_PER_CALL = 50


def fn(path: str, start_page: int = 1, end_page: int = 0) -> str:
    """Extract text from a PDF file.

    Args:
        path: Path to the PDF file.
        start_page: First page to extract (1-indexed, default: 1).
        end_page: Last page to extract (1-indexed, inclusive). 0 = last page.
    """
    try:
        doc = fitz.open(path)
    except Exception as e:
        return f"Error opening PDF: {e}"

    if not doc.is_pdf:
        doc.close()
        return (
            f"Error: '{path}' is not a PDF file. "
            "Use the 'file' tool with action='read' for text files."
        )

    total = len(doc)
    if total == 0:
        doc.close()
        return "Error: PDF has no pages"

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

    for page_num in range(start - 1, end):
        page = doc[page_num]
        text = page.get_text().strip()
        parts.append(f"\n--- Page {page_num + 1} ---\n{text}")

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
