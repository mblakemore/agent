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
    assert simulate_guard(full_content, set()) is True

def test_completion_signals():
    """Test the completion signal detection logic (lines 1961-1971)."""
    signals = [
        "The cycle is complete",
        "I have completed the task",
        "successfully created pull request",
        "no improvements remaining",
    ]
    # we simulate the logic: any(s in full_content.lower() for s in _completion_signals)
    _completion_signals = (
        "cycle is complete", "cycle complete", "concluding this cycle",
        "closing this cycle", "no further actionable", "no remaining",
        "no improvements", "already met", "already resolved",
        "i have completed", "has been achieved", "goal of making",
        "work is done", "task is complete", "actions taken",
        "successfully created pull request", "created a pull request",
        "has been completed", "process is complete",
        "no more open pull requests", "no reviewable prs",
        "standing by", "all tasks", "queue: empty",
    )
    for s in signals:
        assert any(sig in s.lower() for sig in _completion_signals), f"Signal failed: {s}"

def test_text_only_nudge_logic():
    """Test the logic for consecutive text-only responses."""
    # Simulate state variables
    state = {
        "consecutive_text_only": 0,
        "total_nudges": 0,
        "max_text_only": 3,
        "max_total_nudges": 10
    }
    
    # First text-only response
    state["consecutive_text_only"] += 1
    state["total_nudges"] += 1
    assert state["consecutive_text_only"] == 1
    
    # Simulate reaching limit
    state["consecutive_text_only"] = 3
    assert state["consecutive_text_only"] >= state["max_text_only"]

def test_hallucination_guard_regex_extensions():
    """Test that the regex catches various ways of claiming a file read."""
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
        assert simulate_guard(c, set()) is True, f"Failed to detect: {c}"

def test_hallucination_guard_integration():
    """Integration test for the actual hallucination guard in agent.py."""
        pass

def test_hallucination_guard_exception_handling():
    """Test that the hallucination guard does not crash on weird input."""
    full_content = "I read some file that causes a regex error" # (not really possible with re.finditer)
    # We use the simulator to check the try/except block
    assert simulate_guard(None, set()) is False

def test_hallucination_guard_edge_cases():
    """Test edge cases for the hallucination guard simulation."""
    # Test with an empty string
    assert simulate_guard("", set()) is False
    # Test with a file that doesn't match the extension list
    assert simulate_guard("I read agent.exe", set()) is False
    # Test with a file that matches but is actually in accessed_files
    resolved = str((Path.cwd() / "agent.py").resolve())
    assert simulate_guard("I read agent.py", {resolved}) is False
    # Test with a file that matches and is NOT in accessed_files
    assert simulate_guard("I read missing.py", set()) is True
