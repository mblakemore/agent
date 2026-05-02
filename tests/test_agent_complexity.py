"""Tests for _classify_turn_complexity() in agent.py."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import _classify_turn_complexity


def _tool_result_message():
    """Return a message containing a tool_result content block."""
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}],
    }


def test_simple_short_greeting():
    messages = [{"role": "user", "content": "hi"}]
    assert _classify_turn_complexity(messages) == "simple"


def test_simple_short_question():
    messages = [{"role": "user", "content": "What is 2 + 2?"}]
    assert _classify_turn_complexity(messages) == "simple"


def test_standard_with_tool_result():
    messages = [
        _tool_result_message(),
        {"role": "user", "content": "ok thanks"},
    ]
    assert _classify_turn_complexity(messages) == "standard"


def test_standard_medium_user_message():
    # 150-char message, no tools
    text = "a" * 150
    messages = [{"role": "user", "content": text}]
    assert _classify_turn_complexity(messages) == "standard"


def test_extended_keyword_refactor():
    messages = [{"role": "user", "content": "refactor the auth module"}]
    assert _classify_turn_complexity(messages) == "extended"


def test_extended_keyword_implement():
    messages = [{"role": "user", "content": "implement the new API endpoint"}]
    assert _classify_turn_complexity(messages) == "extended"


def test_extended_code_block():
    messages = [{"role": "user", "content": "here is my code:\n```python\nprint('hello')\n```"}]
    assert _classify_turn_complexity(messages) == "extended"


def test_extended_many_tool_results():
    messages = [_tool_result_message() for _ in range(5)]
    messages.append({"role": "user", "content": "done"})
    assert _classify_turn_complexity(messages) == "extended"


def test_extended_long_message():
    # 500-char message
    text = "b" * 500
    messages = [{"role": "user", "content": text}]
    assert _classify_turn_complexity(messages) == "extended"


def test_empty_messages():
    assert _classify_turn_complexity([]) == "simple"
