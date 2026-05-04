import os
import struct
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from tools.read_pdf import fn

def test_read_pdf_open_error():
    with patch('fitz.open') as mock_open:
        mock_open.side_effect = Exception("Permission denied")
        result = fn("dummy.pdf")
        assert "Error opening PDF: Permission denied" in result

def test_read_pdf_empty():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 0
        mock_open.return_value = mock_doc
        result = fn("dummy.pdf")
        assert "Error: PDF has no pages" in result

def test_read_pdf_start_page_exceeds():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 5
        mock_open.return_value = mock_doc
        result = fn("dummy.pdf", start_page=10)
        assert "Error: start_page (10) exceeds page count (5)" in result

def test_read_pdf_happy_path():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 2
        # mock_doc[0] and mock_doc[1]
        mock_page1 = MagicMock()
        mock_page1.get_text.return_value = "Page 1 Content"
        mock_page2 = MagicMock()
        mock_page2.get_text.return_value = "Page 2 Content"
        mock_doc.__getitem__.side_effect = [mock_page1, mock_page2]
        
        mock_open.return_value = mock_doc
        
        result = fn("dummy.pdf")
        assert "[PDF: dummy.pdf | Pages 1-2 of 2]" in result
        assert "--- Page 1 ---\nPage 1 Content" in result
        assert "--- Page 2 ---\nPage 2 Content" in result

def test_read_pdf_paging_cap():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 100
        mock_open.return_value = mock_doc
        
        # Mock many pages
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Content"
        mock_doc.__getitem__.return_value = mock_page
        
        result = fn("dummy.pdf", start_page=1)
        # _MAX_PAGES_PER_CALL = 50
        assert "Pages 1-50 of 100" in result
        assert "[Use read_pdf with start_page=51 to continue reading]" in result

def test_read_pdf_non_pdf_file():
    """read_pdf must reject files that fitz opens but identifies as non-PDF."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = False
        mock_open.return_value = mock_doc
        result = fn("notes.txt")
        assert result.startswith("Error:")
        assert "not a PDF" in result
        assert "file" in result  # directs caller to the file tool
        mock_doc.close.assert_called_once()


def test_read_pdf_custom_range():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 10
        mock_open.return_value = mock_doc

        mock_page = MagicMock()
        mock_page.get_text.return_value = "Range Content"
        mock_doc.__getitem__.return_value = mock_page

        result = fn("dummy.pdf", start_page=2, end_page=4)
        assert "Pages 2-4 of 10" in result
        # Pages 2, 3, 4 = 3 pages total.
        # We can check if mock_doc.__getitem__ was called 3 times.
        assert mock_doc.__getitem__.call_count == 3


def test_read_pdf_inverted_range_returns_error():
    """read_pdf must return a clear error when end_page < start_page."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.__len__.return_value = 10
        mock_open.return_value = mock_doc

        result = fn("dummy.pdf", start_page=5, end_page=3)
        assert result.startswith("Error:")
        assert "end_page" in result
        assert "start_page" in result
        mock_doc.close.assert_called_once()


def test_read_pdf_start_page_zero_returns_error():
    """read_pdf must reject start_page=0; pages are 1-indexed."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.__len__.return_value = 10
        mock_open.return_value = mock_doc

        result = fn("dummy.pdf", start_page=0)
        assert result.startswith("Error:")
        assert "start_page" in result
        assert "1-indexed" in result
        mock_doc.close.assert_called_once()


def test_read_pdf_start_page_negative_returns_error():
    """read_pdf must reject negative start_page values."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.__len__.return_value = 10
        mock_open.return_value = mock_doc

        result = fn("dummy.pdf", start_page=-3)
        assert result.startswith("Error:")
        assert "start_page" in result
        assert "1-indexed" in result
        mock_doc.close.assert_called_once()


def test_read_pdf_end_page_negative_returns_error():
    """read_pdf must reject negative end_page values (not silently treat as last page)."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.__len__.return_value = 3
        mock_open.return_value = mock_doc

        result = fn("dummy.pdf", start_page=1, end_page=-5)
        assert result.startswith("Error:")
        assert "end_page" in result
        assert "-5" in result
        mock_doc.close.assert_called_once()


def test_read_pdf_end_page_exceeds_page_count_returns_error():
    """read_pdf must reject end_page beyond the document length (not silently clamp)."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.__len__.return_value = 3
        mock_open.return_value = mock_doc

        result = fn("dummy.pdf", start_page=1, end_page=999)
        assert result.startswith("Error:")
        assert "end_page" in result
        assert "999" in result
        assert "3" in result  # mentions actual page count
        mock_doc.close.assert_called_once()


def test_read_pdf_end_page_zero_means_last_page():
    """end_page=0 (the default sentinel) must still return all pages successfully."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.__len__.return_value = 2
        mock_page = MagicMock()
        mock_page.get_text.return_value = "content"
        mock_doc.__getitem__.return_value = mock_page
        mock_open.return_value = mock_doc

        result = fn("dummy.pdf", start_page=1, end_page=0)
        assert "Pages 1-2 of 2" in result
        assert "Error" not in result


def test_read_pdf_end_page_exact_last_page():
    """end_page equal to the exact page count must succeed."""
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.__len__.return_value = 5
        mock_page = MagicMock()
        mock_page.get_text.return_value = "content"
        mock_doc.__getitem__.return_value = mock_page
        mock_open.return_value = mock_doc

        result = fn("dummy.pdf", start_page=1, end_page=5)
        assert "Pages 1-5 of 5" in result
        assert "Error" not in result


# ── wrong-type page argument tests (#680) ─────────────────────────────────────

def _mock_pdf_doc(total_pages=5):
    """Return a pre-configured MagicMock that looks like a fitz PDF document."""
    doc = MagicMock()
    doc.is_pdf = True
    doc.__len__ = MagicMock(return_value=total_pages)
    page = MagicMock()
    page.get_text.return_value = "content"
    doc.__getitem__.return_value = page
    return doc


@patch("fitz.open")
def test_read_pdf_string_start_page_coerced(mock_open):
    """start_page='2' (stringified int) must be coerced, not raise TypeError (#680)."""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page='2')
    assert "Error" not in result
    assert "Pages 2-" in result


@patch("fitz.open")
def test_read_pdf_string_end_page_coerced(mock_open):
    """end_page='3' (stringified int) must be coerced, not raise TypeError (#680)."""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page='3')
    assert "Error" not in result
    assert "Pages 1-3 of 5" in result


@patch("fitz.open")
def test_read_pdf_non_numeric_start_page_returns_error(mock_open):
    """start_page='bad' must return a clean Error string, not crash (#680)."""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page='bad')
    assert result.startswith("Error: start_page must be an integer")
    assert "'str'" in result


@patch("fitz.open")
def test_read_pdf_non_numeric_end_page_returns_error(mock_open):
    """end_page='bad' must return a clean Error string, not crash (#680)."""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page='bad')
    assert result.startswith("Error: end_page must be an integer")
    assert "'str'" in result


# ── Issue #776: real-file edge cases must return Error strings, not crash ─────


def test_read_pdf_plain_text_with_pdf_extension_returns_error():
    """A plain-text file with a .pdf extension must return an Error string, not raise. (#776)"""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.pdf', delete=False)
    f.write("This is not a PDF\n")
    f.close()
    try:
        result = fn(path=f.name)
        assert isinstance(result, str), "Must return a string, not raise"
        assert result.startswith("Error"), f"Expected Error string, got: {result!r}"
    finally:
        os.unlink(f.name)


def test_read_pdf_empty_file_returns_error():
    """An empty file with a .pdf extension must return an Error string, not raise. (#776)"""
    f = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    f.close()
    try:
        result = fn(path=f.name)
        assert isinstance(result, str), "Must return a string, not raise"
        assert result.startswith("Error"), f"Expected Error string, got: {result!r}"
    finally:
        os.unlink(f.name)


def test_read_pdf_binary_non_pdf_returns_error():
    """A JPEG binary renamed to .pdf must return an Error string, not raise. (#776)"""
    f = tempfile.NamedTemporaryFile(mode='wb', suffix='.pdf', delete=False)
    f.write(b'\xff\xd8\xff\xe0' + b'\x00' * 100)  # JPEG magic bytes
    f.close()
    try:
        result = fn(path=f.name)
        assert isinstance(result, str), "Must return a string, not raise"
        assert result.startswith("Error"), f"Expected Error string, got: {result!r}"
    finally:
        os.unlink(f.name)


def test_read_pdf_corrupted_pdf_header_returns_error():
    """A file with the PDF magic header but corrupt body must return an Error, not raise. (#776)"""
    f = tempfile.NamedTemporaryFile(mode='wb', suffix='.pdf', delete=False)
    f.write(b'%PDF-1.4\n%corrupted content here\n')
    f.close()
    try:
        result = fn(path=f.name)
        assert isinstance(result, str), "Must return a string, not raise"
        assert result.startswith("Error"), f"Expected Error string, got: {result!r}"
    finally:
        os.unlink(f.name)


def test_read_pdf_non_pdf_extension_returns_error():
    """A file without a .pdf extension must return an Error string, not raise. (#776)"""
    f = tempfile.NamedTemporaryFile(suffix='.txt', delete=False)
    f.write(b'not a pdf')
    f.close()
    try:
        result = fn(path=f.name)
        assert isinstance(result, str), "Must return a string, not raise"
        assert result.startswith("Error"), f"Expected Error string, got: {result!r}"
    finally:
        os.unlink(f.name)
