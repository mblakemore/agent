import pytest
from agent import _ReasoningRenderer, theme
from unittest.mock import MagicMock

class MockWriter:
    def __init__(self):
        self.output = ""
    def __call__(self, text):
        self.output += text

def test_think_parser_full_cycle():
    writer = MockWriter()
    renderer = _ReasoningRenderer(writer)
    
    # <think>I am thinking</think>Done.
    chunks = ["<th", "ink>I am thin", "king</think>", "Done."]
    for chunk in chunks:
        renderer.feed(chunk)
    renderer.flush()
    
    assert "[Reasoning]" in writer.output
    assert "[/Reasoning]" in writer.output
    assert "I am thinking" in writer.output
    assert "Done." in writer.output

def test_think_parser_flush_while_thinking():
    writer = MockWriter()
    renderer = _ReasoningRenderer(writer)
    
    # <think>I am still thinking...
    renderer.feed("<think>I am still thinking...")
    renderer.flush()
    
    assert "[Reasoning]" in writer.output
    assert "I am still thinking..." in writer.output
    assert "[/Reasoning]" in writer.output

def test_think_parser_empty_block():
    writer = MockWriter()
    renderer = _ReasoningRenderer(writer)
    
    # <think></think>
    renderer.feed("<think></think>")
    renderer.flush()
    
    assert "[Reasoning]" in writer.output
    assert "[/Reasoning]" in writer.output

def test_think_parser_split_tags():
    writer = MockWriter()
    renderer = _ReasoningRenderer(writer)
    
    # Test split <think> tag
    renderer.feed("<thi")
    assert writer.output == "" # Should be pending, NOT flushed
    
    # Complete the tag
    renderer.feed("nk>")
    assert "[Reasoning]" in writer.output

def test_think_parser_max_pending():
    writer = MockWriter()
    renderer = _ReasoningRenderer(writer)
    
    # _MAX_PENDING is 7. Send 10 chars.
    # It should emit 10-7 = 3 chars and keep 7.
    renderer.feed("1234567890")
    assert writer.output == "123"
    
    renderer.flush()
    assert writer.output == "1234567890"

def test_think_parser_flush_partial():
    writer = MockWriter()
    renderer = _ReasoningRenderer(writer)
    
    renderer.feed("<thi")
    assert writer.output == ""
    
    renderer.flush()
    assert writer.output == "<thi"

def test_think_parser_multiple_blocks():
    writer = MockWriter()
    renderer = _ReasoningRenderer(writer)
    
    # <think>1</think>Text<think>2</think>
    renderer.feed("<think>1</think>Text<think>2</think>")
    renderer.flush()
    
    assert "[Reasoning]" in writer.output
    assert "1" in writer.output
    assert "[/Reasoning]" in writer.output
    assert "Text" in writer.output
    assert "[Reasoning]" in writer.output
    assert "2" in writer.output
    assert "[/Reasoning]" in writer.output
