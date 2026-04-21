import pytest
import re
from pathlib import Path
from unittest.mock import patch, MagicMock
from agent import _detect_hallucinated_read

def test_detect_hallucination_fake_read():
    """Test that a claim to read a file is a hallucination if it wasn't accessed."""
    with patch('tools.file._accessed_files', set()):
        content = "I have read the contents of agent.py and found a bug."
        is_hallucinated, reason = _detect_hallucinated_read(content)
        assert is_hallucinated is True
        assert "agent.py" in reason

def test_detect_hallucination_legitimate_read():
    """Test that a claim to read a file is NOT a hallucination if it was actually read."""
    filename = "agent.py"
    resolved_path = str((Path.cwd() / filename).resolve())
    with patch('tools.file._accessed_files', {resolved_path}):
        content = f"I have read the contents of {filename} and found a bug."
        is_hallucinated, reason = _detect_hallucinated_read(content)
        assert is_hallucinated is False

def test_detect_hallucination_ignores_intent():
    """Test that saying 'I will read' does not trigger the guard."""
    with patch('tools.file._accessed_files', set()):
        cases = [
            "I will now read agent.py to verify the fix.",
            "I need to read agent.py",
            "I should read agent.py",
            "I must read agent.py",
            "I am going to read agent.py",
        ]
        for content in cases:
            is_hallucinated, reason = _detect_hallucinated_read(content)
            assert is_hallucinated is False, f"Failed on: {content}"

def test_detect_hallucination_actual_claim_still_detected():
    """Test that actual claims are still detected even with intent markers elsewhere."""
    with patch('tools.file._accessed_files', set()):
        content = "I will read tools.py, but I have already read agent.py."
        is_hallucinated, reason = _detect_hallucinated_read(content)
        assert is_hallucinated is True
        assert "agent.py" in reason

def test_detect_hallucination_regex_variants():
    """Test various ways of claiming a file read."""
    with patch('tools.file._accessed_files', set()):
        cases = [
            "found agent.py",
            "contents of tools.py",
            "file has agent.py",
            "file contains tools.py",
            "file shows agent.py",
            "read 'agent.py'",
            "read \"tools.py\"",
            "read `agent.py`",
        ]
        for c in cases:
            is_hallucinated, reason = _detect_hallucinated_read(c)
            assert is_hallucinated is True, f"Failed to detect: {c}"

def test_detect_hallucination_empty_or_none():
    """Test that None or empty strings don't trigger the guard."""
    assert _detect_hallucinated_read(None)[0] is False
    assert _detect_hallucinated_read("")[0] is False

def test_detect_hallucination_invalid_extension():
    """Test that files without matching extensions are ignored."""
    with patch('tools.file._accessed_files', set()):
        content = "I read agent.exe"
        is_hallucinated, reason = _detect_hallucinated_read(content)
        assert is_hallucinated is False

    def test_detect_hallucination_exception_handling():
        """Test that the guard doesn't crash if imports fail."""
        # When we patch tools.file._accessed_files to raise ImportError,
        # the try/except block in _detect_hallucinated_read should catch it
        # and return (False, None).
        with patch('tools.file._accessed_files', side_effect=ImportError):
            content = "I read agent.py"
            is_hallucinated, reason = _detect_hallucinated_read(content)
            assert is_hallucinated is False
            assert reason is None
