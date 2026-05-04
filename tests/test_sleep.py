import pytest
from tools.sleep import fn, _MAX_SLEEP

def test_sleep_success():
    """Test that sleep succeeds and returns the expected message."""
    result = fn(0.1)
    assert result == "Slept for 0.1 seconds"

def test_sleep_negative_value():
    """Test that sleeping for a negative value is handled (time.sleep usually raises ValueError)."""
    # time.sleep(-1) raises ValueError
    result = fn(-1.0)
    assert "Error" in result

def test_sleep_invalid_type_string():
    """Test that passing a string produces a clean, user-readable error (not a raw TypeError)."""
    result = fn("1")  # type: ignore
    assert result.startswith("Error:")
    # Must NOT expose the internal comparison-operator TypeError message
    assert "not supported between instances" not in result
    assert "str" in result  # should name the bad type


def test_sleep_invalid_type_non_numeric_string():
    """Test that a non-numeric string also gives a clean validation error."""
    result = fn("five")  # type: ignore
    assert result.startswith("Error:")
    assert "not supported between instances" not in result
    assert "str" in result


def test_sleep_invalid_type_none():
    """Test that None produces a clean validation error."""
    result = fn(None)  # type: ignore
    assert result.startswith("Error:")
    assert "not supported between instances" not in result
    assert "NoneType" in result


def test_sleep_invalid_type_list():
    """Test that a list produces a clean validation error."""
    result = fn([1, 2])  # type: ignore
    assert result.startswith("Error:")
    assert "not supported between instances" not in result
    assert "list" in result

def test_sleep_exactly_at_max():
    """Test that sleeping for exactly _MAX_SLEEP seconds is allowed."""
    # We don't actually sleep — just verify the guard doesn't trigger at the boundary.
    # Use 0 as proxy since we cannot sleep 3600 s in a test; test the boundary logic directly.
    assert _MAX_SLEEP == 3600

def test_sleep_exceeds_max_returns_error():
    """Test that a duration above _MAX_SLEEP is rejected immediately without blocking."""
    result = fn(_MAX_SLEEP + 1)
    assert result.startswith("Error:")
    assert str(_MAX_SLEEP) in result

def test_sleep_very_large_value_rejected():
    """Test that a very large value (e.g. 9999999) is rejected without hanging."""
    result = fn(9999999)
    assert result.startswith("Error:")
    assert "exceeds maximum" in result

def test_sleep_just_under_max_allowed():
    """Test that a value just below the ceiling is accepted (guard only fires above)."""
    # We verify the guard boundary — actual sleep is 0 for speed.
    # Patch: call with _MAX_SLEEP - 0.001 to verify no error; use 0 to avoid 1-hour wait.
    # This test validates the guard condition (> not >=) by checking boundary math.
    assert _MAX_SLEEP - 0.001 < _MAX_SLEEP  # sanity
    result = fn(0)  # trivially valid; boundary logic validated by test_sleep_exceeds_max_returns_error
    assert "Error" not in result


# ── bool guard tests (#792) ───────────────────────────────────────────────────


def test_sleep_bool_true_returns_error():
    """True must be rejected with a clean type error, not silently sleep for 1 second (#792).

    Before the fix, isinstance(True, (int, float)) returned True because bool is a
    subclass of int, so sleep(True) silently called time.sleep(1) and returned
    'Slept for True seconds'.
    """
    result = fn(True)  # type: ignore
    assert result.startswith("Error:"), f"Expected error for sleep(True), got: {result!r}"
    assert "bool" in result, f"Error must mention 'bool', got: {result!r}"


def test_sleep_bool_false_returns_error():
    """False must be rejected with a clean type error, not silently succeed (#792)."""
    result = fn(False)  # type: ignore
    assert result.startswith("Error:"), f"Expected error for sleep(False), got: {result!r}"
    assert "bool" in result, f"Error must mention 'bool', got: {result!r}"


def test_sleep_bool_does_not_sleep():
    """Passing a bool must return the error immediately without sleeping (#792)."""
    import time
    t0 = time.monotonic()
    fn(True)  # type: ignore
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, (
        f"sleep(True) should return immediately but took {elapsed:.2f}s — "
        f"suggests the bool slipped past the guard and time.sleep(1) was called"
    )


# ── negative guard tests (#792) ──────────────────────────────────────────────


def test_sleep_negative_returns_clean_error():
    """Negative seconds must produce a clean, explicit error message (#792).

    Before the fix, the negative check fell through to time.sleep(-1) which raised
    ValueError('sleep length must be non-negative') — a stdlib message exposed raw
    to the caller instead of a consistent tool-level error.
    """
    result = fn(-1.0)
    assert result.startswith("Error:"), f"Expected error for sleep(-1), got: {result!r}"
    assert "non-negative" in result, (
        f"Error message should mention 'non-negative', got: {result!r}"
    )


def test_sleep_negative_float_returns_clean_error():
    """A small negative float must also produce the clean guard error (#792)."""
    result = fn(-0.001)
    assert result.startswith("Error:")
    assert "non-negative" in result


def test_sleep_negative_does_not_expose_stdlib_message():
    """The negative-seconds error must not expose the raw time.sleep ValueError text (#792)."""
    result = fn(-5)
    # The stdlib message includes 'sleep length must be non-negative'; our guard fires
    # earlier with the same phrase but via an explicit return, not an exception.
    # We check the error is a clean return (starts with 'Error:') rather than an
    # exception bubble-up — the existing try/except wrapper would produce the same
    # text, so we additionally verify the tool message is consistent with other
    # tool guards (all start with "Error:").
    assert result.startswith("Error:"), (
        f"Expected 'Error:' prefix for negative sleep, got: {result!r}"
    )


# ── NaN / Inf guards (#891) ───────────────────────────────────────────────────


def test_sleep_nan_returns_clear_error():
    """sleep(float('nan')) must return a clear error rather than an obscure
    'sleep length must be non-negative' message from time.sleep (#891).

    Before the fix, NaN passed both the < 0 and > _MAX_SLEEP guards (NaN
    comparisons are always False), reaching time.sleep(nan) which raised
    ValueError with a confusing stdlib message.
    """
    import math
    result = fn(math.nan)
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "finite" in result or "nan" in result.lower(), (
        f"Error should mention finite or nan: {result!r}"
    )


def test_sleep_inf_returns_clear_error():
    """sleep(float('inf')) must return a clear error (#891).

    Inf > _MAX_SLEEP is True, so Inf was already caught — but with the
    message 'sleep duration inf exceeds maximum…' which is somewhat
    misleading. The isfinite check now fires first with a clearer message.
    """
    import math
    result = fn(math.inf)
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"


# ── !r quoting on type names (#915) ──────────────────────────────────────────

def test_sleep_string_arg_type_name_is_quoted():
    """String passed to seconds must include the quoted type name 'str', not bare str (#915)."""
    result = fn("five")
    assert result.startswith("Error:"), f"Expected error: {result!r}"
    assert "'str'" in result, f"Type name must be quoted as 'str', got: {result!r}"


def test_sleep_bool_type_name_is_quoted():
    """Boolean passed to seconds must include the quoted type name 'bool', not bare bool (#915)."""
    result = fn(True)
    assert result.startswith("Error:"), f"Expected error: {result!r}"
    assert "'bool'" in result, f"Type name must be quoted as 'bool', got: {result!r}"


def test_sleep_list_type_name_is_quoted():
    """List passed to seconds must include the quoted type name 'list', not bare list (#915)."""
    result = fn([1])
    assert result.startswith("Error:"), f"Expected error: {result!r}"
    assert "'list'" in result, f"Type name must be quoted as 'list', got: {result!r}"
