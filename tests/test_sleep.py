import pytest
from tools.sleep import fn

def test_sleep_success():
    """Test that sleep succeeds and returns the expected message."""
    result = fn(0.1)
    assert result == "Slept for 0.1 seconds"

def test_sleep_negative_value():
    """Test that sleeping for a negative value is handled (time.sleep usually raises ValueError)."""
    # time.sleep(-1) raises ValueError
    result = fn(-1.0)
    assert "Error" in result

def test_sleep_invalid_type():
    """Test that passing a non-number raises an error."""
    # Passing a string to time.sleep raises TypeError
    result = fn("1") # type: ignore
    assert "Error" in result
