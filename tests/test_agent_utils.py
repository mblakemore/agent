import pytest
from agent import _extract_pinned

def test_extract_pinned_no_pinned():
    text = "This is a normal message without any pinned instructions."
    cleaned, pinned = _extract_pinned(text)
    assert cleaned == "This is a normal message without any pinned instructions."
    assert pinned == ""

def test_extract_pinned_single_block():
    text = "Hello <pinned>Keep this important\ninstruction</pinned> World"
    cleaned, pinned = _extract_pinned(text)
    # Note: _extract_pinned uses .strip() on the result of .sub(), but doesn't
    # collapse internal double spaces.
    assert cleaned == "Hello  World"
    assert pinned == "Keep this important\ninstruction"

def test_extract_pinned_multiple_blocks():
    text = "Start <pinned>Block 1</pinned> Middle <pinned>Block 2</pinned> End"
    cleaned, pinned = _extract_pinned(text)
    assert cleaned == "Start  Middle  End"
    assert pinned == "Block 1\nBlock 2"

def test_extract_pinned_whitespace_handling():
    text = "  <pinned>  Trim me  </pinned>  "
    cleaned, pinned = _extract_pinned(text)
    assert cleaned == ""
    assert pinned == "Trim me"
