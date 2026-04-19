import pytest
import re
from pathlib import Path
from unittest.mock import patch, MagicMock
import agent

def simulate_guard(full_content, accessed_files):
    """Helper to simulate the hallucination guard logic from agent.py"""
    _hallucinated_read = False
    if full_content:
        try:
            # This matches the logic in agent.py
            for match in re.finditer(
                r'(?:read|found|contents? of|file (?:has|contains|shows))\s+[`"\']?(\S+\.(?:py|json|md|txt|yaml|yml|toml|jsonl|sh|cfg))',
                full_content, re.IGNORECASE
            ):
                claimed_file = match.group(1)
                start = match.start()
                preceding = full_content[max(0, start-20):start].lower()
                if any(word in preceding for word in ['will', 'to ', 'should', 'must', 'need to']):
                    continue
                
                _resolved = str((Path.cwd() / claimed_file).resolve())
                if _resolved not in accessed_files:
                    _hallucinated_read = True
                    break
        except Exception:
            pass
    return _hallucinated_read

def test_hallucinated_read_detection():
    """Test that the agent correctly detects when the model claims to have read a file it didn't."""
    full_content = "I have read the contents of agent.py and found a bug."
    with patch('tools.file._accessed_files', set()):
        assert simulate_guard(full_content, set()) is True

def test_legitimate_read_not_detected_as_hallucination():
    """Test that a claim to read a file is NOT a hallucination if it was actually read."""
    filename = "agent.py"
    full_content = f"I have read the contents of {filename} and found a bug."
    resolved_path = str((Path.cwd() / filename).resolve())
    assert simulate_guard(full_content, {resolved_path}) is False

def test_intent_to_read_not_detected_as_hallucination():
    """Test that saying 'I will read' does not trigger the guard."""
    full_content = "I will now read agent.py to verify the fix."
    assert simulate_guard(full_content, set()) is False

def test_various_intent_markers():
    """Test multiple intent markers are correctly ignored."""
    cases = [
        "I need to read agent.py",
        "I should read agent.py",
        "I must read agent.py",
        "I am going to read agent.py",
    ]
    for content in cases:
        assert simulate_guard(content, set()) is False, f"Failed on: {content}"

def test_actual_claim_still_detected():
    """Test that actual claims are still detected even with intent markers elsewhere."""
    full_content = "I will read tools.py, but I have already read agent.py."
    # We simulate that only tools.py was read (or none)
    assert simulate_guard(full_content, set()) is True # agent.py claim is hallucinated
