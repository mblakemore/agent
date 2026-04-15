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
