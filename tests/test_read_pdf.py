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
        assert "Error: opening PDF: Permission denied" in result

def test_read_pdf_empty():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.needs_pass = 0
        mock_doc.__len__.return_value = 0
        mock_open.return_value = mock_doc
        result = fn("dummy.pdf")
        assert "Error: PDF has no pages" in result

def test_read_pdf_start_page_exceeds():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.needs_pass = 0
        mock_doc.__len__.return_value = 5
        mock_open.return_value = mock_doc
        result = fn("dummy.pdf", start_page=10)
        assert "Error: start_page (10) exceeds page count (5)" in result

def test_read_pdf_happy_path():
    with patch('fitz.open') as mock_open:
        mock_doc = MagicMock()
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
        mock_doc.needs_pass = 0
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
    doc.needs_pass = 0
    doc.__len__ = MagicMock(return_value=total_pages)
    page = MagicMock()
    page.get_text.return_value = "content"
    doc.__getitem__.return_value = page
    return doc


@patch("fitz.open")
def test_read_pdf_string_start_page_rejected(mock_open):
    """start_page='2' must now return a clear type error rather than silently coerce (#905).

    Before #905, int('2') succeeded in the coercion block and start_page was
    silently treated as 2.  Now strings are caught by an explicit guard.
    """
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page='2')
    assert result.startswith("Error:"), f"Expected error for start_page='2': {result!r}"
    assert "str" in result, f"Error must mention 'str': {result!r}"
    assert "quote" in result.lower() or "without" in result.lower(), (
        f"Error should hint about removing quotes: {result!r}"
    )


@patch("fitz.open")
def test_read_pdf_string_end_page_rejected(mock_open):
    """end_page='3' must now return a clear type error rather than silently coerce (#905)."""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page='3')
    assert result.startswith("Error:"), f"Expected error for end_page='3': {result!r}"
    assert "str" in result, f"Error must mention 'str': {result!r}"


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


# ── Issue #792: bool page arguments must be rejected explicitly ────────────────
# Booleans are a subclass of int in Python; True==1 and False==0.  Without an
# explicit guard, start_page=True would silently read page 1 and start_page=False
# would produce a confusing "start_page (0) is invalid" message instead of a clear
# type error.  These tests document and lock in the explicit-rejection behaviour.


@patch("fitz.open")
def test_read_pdf_bool_true_start_page_returns_error(mock_open):
    """start_page=True must return a clear type error, not silently read page 1. (#792)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=True)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention bool type, got: {result!r}"
    assert "start_page" in result


@patch("fitz.open")
def test_read_pdf_bool_false_start_page_returns_error(mock_open):
    """start_page=False must return a clear type error, not a '< 1' message. (#792)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=False)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention bool type, got: {result!r}"
    assert "start_page" in result


@patch("fitz.open")
def test_read_pdf_bool_true_end_page_returns_error(mock_open):
    """end_page=True must return a clear type error, not silently set end_page=1. (#792)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page=True)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention bool type, got: {result!r}"
    assert "end_page" in result


@patch("fitz.open")
def test_read_pdf_bool_false_end_page_returns_error(mock_open):
    """end_page=False must return a clear type error, not silently treat as end_page=0. (#792)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page=False)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention bool type, got: {result!r}"
    assert "end_page" in result


@patch("fitz.open")
def test_read_pdf_bool_start_page_doc_closed(mock_open):
    """doc.close() must be called before returning a bool-type error. (#792)"""
    mock_doc = _mock_pdf_doc()
    mock_open.return_value = mock_doc
    fn("dummy.pdf", start_page=True)
    mock_doc.close.assert_called_once()


@patch("fitz.open")
def test_read_pdf_integer_start_page_unaffected_by_bool_guard(mock_open):
    """Plain integer start_page must still work correctly after the bool guard. (#792)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=2)
    assert "Error" not in result
    assert "Pages 2-" in result


# ── Issue #803: fractional float page numbers must be rejected, whole floats coerced ──
# Fractional floats like 1.5 would silently truncate to int(1) = 1, reading the
# wrong page without any indication of the error.  Whole-number floats like 2.0
# are safe to coerce, consistent with the task_tracker float-guard pattern.


@patch("fitz.open")
def test_read_pdf_fractional_float_start_page_returns_error(mock_open):
    """start_page=1.5 must return a clear error, not silently read page 1. (#803)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1.5)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "non-integer float" in result, f"Error must mention non-integer float, got: {result!r}"
    assert "start_page" in result
    assert "1.5" in result


@patch("fitz.open")
def test_read_pdf_fractional_float_start_page_suggests_neighbors(mock_open):
    """Error for start_page=1.5 must suggest 1 and 2 as likely intended values. (#803)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1.5)
    assert "1" in result and "2" in result, f"Error must suggest neighbors, got: {result!r}"


@patch("fitz.open")
def test_read_pdf_whole_float_start_page_coerced(mock_open):
    """start_page=2.0 (whole-number float) must be coerced to 2, not rejected. (#803)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=2.0)
    assert "Error" not in result, f"Unexpected error for whole float 2.0: {result!r}"
    assert "Pages 2-" in result


@patch("fitz.open")
def test_read_pdf_fractional_float_end_page_returns_error(mock_open):
    """end_page=3.7 must return a clear error, not silently set end_page=3. (#803)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page=3.7)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "non-integer float" in result, f"Error must mention non-integer float, got: {result!r}"
    assert "end_page" in result
    assert "3.7" in result


@patch("fitz.open")
def test_read_pdf_whole_float_end_page_coerced(mock_open):
    """end_page=3.0 (whole-number float) must be coerced to 3, not rejected. (#803)"""
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page=3.0)
    assert "Error" not in result, f"Unexpected error for whole float 3.0: {result!r}"
    assert "Pages 1-3 of 5" in result


@patch("fitz.open")
def test_read_pdf_fractional_float_start_page_doc_closed(mock_open):
    """doc.close() must be called before returning a fractional-float error. (#803)"""
    mock_doc = _mock_pdf_doc()
    mock_open.return_value = mock_doc
    fn("dummy.pdf", start_page=1.5)
    mock_doc.close.assert_called_once()


@patch("fitz.open")
def test_read_pdf_fractional_float_end_page_doc_closed(mock_open):
    """doc.close() must be called before returning a fractional end_page float error. (#803)"""
    mock_doc = _mock_pdf_doc()
    mock_open.return_value = mock_doc
    fn("dummy.pdf", start_page=1, end_page=2.9)
    mock_doc.close.assert_called_once()


# ── None path guard (#809) ─────────────────────────────────────────────────────
# fitz.open(None) silently opens an empty in-memory document (is_pdf=True, 0 pages).
# Without a type guard, fn(path=None) returns the misleading "Error: PDF has no pages"
# instead of a clear type error. The guard must fire before fitz.open() is called so
# that fitz is never invoked with a non-string path.


def test_read_pdf_none_path_returns_type_error():
    """path=None must return a clear Error string without calling fitz.open. (#809)"""
    result = fn(path=None)
    assert isinstance(result, str), "Must return a string, not raise"
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "path" in result
    assert "NoneType" in result


def test_read_pdf_integer_path_returns_type_error():
    """path=123 (non-string) must return a clear Error string without calling fitz.open. (#809)"""
    result = fn(path=123)
    assert isinstance(result, str), "Must return a string, not raise"
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "path" in result
    assert "int" in result


@patch("fitz.open")
def test_read_pdf_none_path_does_not_call_fitz(mock_open):
    """fitz.open must not be called when path is None. (#809)"""
    fn(path=None)
    mock_open.assert_not_called()


def test_read_pdf_open_exception_error_format():
    """fitz.open exception must produce 'Error: opening PDF: ...' (not 'Error opening PDF: ...')."""
    with patch("fitz.open", side_effect=Exception("disk I/O error")):
        result = fn("dummy.pdf")
    assert isinstance(result, str)
    assert result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}"
    assert "opening PDF" in result, f"Expected 'opening PDF' in message, got: {result!r}"
    assert "disk I/O error" in result


# ── Issue #824: encrypted/password-protected PDFs must return Error, not raise ──
# fitz.open() succeeds on encrypted PDFs but leaves doc.needs_pass=1.
# Accessing pages then raises ValueError: document closed or encrypted.
# The fix detects needs_pass before entering the page loop.


@patch("fitz.open")
def test_read_pdf_encrypted_returns_error(mock_open):
    """read_pdf must return a clean Error for encrypted PDFs, not raise ValueError. (#824)"""
    mock_doc = MagicMock()
    mock_doc.is_pdf = True
    mock_doc.needs_pass = 1
    mock_open.return_value = mock_doc

    result = fn("secret.pdf")
    assert isinstance(result, str), "Must return a string, not raise"
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "encrypted" in result.lower() or "password" in result.lower(), (
        f"Error must mention encryption or password, got: {result!r}"
    )


@patch("fitz.open")
def test_read_pdf_encrypted_doc_closed(mock_open):
    """doc.close() must be called before returning the encrypted-PDF error. (#824)"""
    mock_doc = MagicMock()
    mock_doc.is_pdf = True
    mock_doc.needs_pass = 1
    mock_open.return_value = mock_doc

    fn("secret.pdf")
    mock_doc.close.assert_called_once()


@patch("fitz.open")
def test_read_pdf_encrypted_with_page_range_returns_error(mock_open):
    """Encrypted PDFs must return Error even when start_page/end_page are provided. (#824)"""
    mock_doc = MagicMock()
    mock_doc.is_pdf = True
    mock_doc.needs_pass = 1
    mock_open.return_value = mock_doc

    result = fn("secret.pdf", start_page=1, end_page=1)
    assert isinstance(result, str), "Must return a string, not raise"
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"


@patch("fitz.open")
def test_read_pdf_page_extraction_exception_returns_error(mock_open):
    """Unexpected exception during page extraction must return Error, not propagate. (#824)"""
    mock_doc = MagicMock()
    mock_doc.is_pdf = True
    mock_doc.needs_pass = 0
    mock_doc.__len__ = MagicMock(return_value=3)
    mock_doc.__getitem__.side_effect = RuntimeError("internal fitz error")
    mock_open.return_value = mock_doc

    result = fn("broken.pdf")
    assert isinstance(result, str), "Must return a string, not raise"
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "reading PDF page" in result or "internal fitz error" in result


# ── Path confinement and null-byte validation (#872) ──────────────────────────

class TestReadPdfPathConfinement:
    """read_pdf must reject paths outside the working directory (#872)."""

    def setup_method(self):
        self._orig_cwd = os.getcwd()
        self._outer = tempfile.mkdtemp()
        import pathlib
        self._project = pathlib.Path(self._outer) / "project"
        self._project.mkdir()
        os.chdir(str(self._project))

    def teardown_method(self):
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._outer, ignore_errors=True)

    def test_absolute_path_outside_cwd_returns_error(self):
        """read_pdf with an absolute path outside cwd must be rejected. (#872)"""
        result = fn(path=self._outer)
        assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
        assert "outside" in result

    def test_relative_traversal_outside_cwd_returns_error(self):
        """read_pdf with '../' traversal outside cwd must be rejected. (#872)"""
        result = fn(path="../escape.pdf")
        assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
        assert "outside" in result

    def test_null_byte_in_path_returns_error(self):
        """read_pdf with a null byte in path must return Error, not crash. (#872)"""
        result = fn(path="/tmp/test\x00.pdf")
        assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
        assert "null byte" in result

    @patch("fitz.open")
    def test_valid_path_inside_cwd_still_works(self, mock_open):
        """read_pdf with a valid path inside cwd must not be rejected. (#872)"""
        import pathlib
        mock_doc = MagicMock()
        mock_doc.is_pdf = True
        mock_doc.needs_pass = 0
        mock_doc.__len__ = MagicMock(return_value=1)
        page = MagicMock()
        page.get_text.return_value = "page content"
        mock_doc.__getitem__ = MagicMock(return_value=page)
        mock_open.return_value = mock_doc

        inside = str(self._project / "doc.pdf")
        result = fn(path=inside)
        assert not result.startswith("Error:"), f"Expected success for cwd path, got: {result!r}"


# ── NaN / Inf page guards (#903) ──────────────────────────────────────────────


@patch("fitz.open")
def test_read_pdf_inf_start_page_returns_clear_error(mock_open):
    """start_page=float('inf') must return a clear error, not OverflowError (#903).

    Before the fix, int(inf) raised OverflowError which was not caught by
    except (TypeError, ValueError) and propagated as an unhandled exception.
    """
    import math
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=math.inf)
    assert result.startswith("Error:"), f"Expected error: {result!r}"
    assert "finite" in result or "inf" in result.lower(), (
        f"Error should mention finite or inf: {result!r}"
    )


@patch("fitz.open")
def test_read_pdf_nan_start_page_returns_clear_error(mock_open):
    """start_page=float('nan') must return a clear error (#903)."""
    import math
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=math.nan)
    assert result.startswith("Error:"), f"Expected error: {result!r}"


@patch("fitz.open")
def test_read_pdf_inf_end_page_returns_clear_error(mock_open):
    """end_page=float('inf') must return a clear error, not OverflowError (#903)."""
    import math
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page=math.inf)
    assert result.startswith("Error:"), f"Expected error: {result!r}"
    assert "finite" in result or "inf" in result.lower(), (
        f"Error should mention finite or inf: {result!r}"
    )


@patch("fitz.open")
def test_read_pdf_nan_end_page_returns_clear_error(mock_open):
    """end_page=float('nan') must return a clear error (#903)."""
    import math
    mock_open.return_value = _mock_pdf_doc()
    result = fn("dummy.pdf", start_page=1, end_page=math.nan)
    assert result.startswith("Error:"), f"Expected error: {result!r}"


# ── !r quoting on type names (#915) ──────────────────────────────────────────

def test_read_pdf_non_string_path_type_name_is_quoted():
    """Non-string path error must include quoted type name 'int', not bare int (#915)."""
    result = fn(42)
    assert result.startswith("Error:"), f"Expected error: {result!r}"
    assert "'int'" in result, f"Type name must be quoted as 'int', got: {result!r}"


def test_read_pdf_none_path_type_name_is_quoted():
    """None path error must include quoted type name 'NoneType' (#915)."""
    result = fn(None)
    assert result.startswith("Error:"), f"Expected error: {result!r}"
    assert "'NoneType'" in result, f"Type name must be quoted as 'NoneType', got: {result!r}"
