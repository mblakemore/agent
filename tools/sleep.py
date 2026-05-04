"""Sleep for a specified duration."""

import math
import time

_MAX_SLEEP = 3600  # 1 hour ceiling — prevent indefinite process hang


def fn(seconds: float) -> str:
    """Sleep for the given number of seconds.

    Args:
        seconds: Number of seconds to sleep. Must be between 0 and 3600.
    """
    if isinstance(seconds, bool):
        return f"Error: 'seconds' must be a number, got {type(seconds).__name__}"
    if not isinstance(seconds, (int, float)):
        return f"Error: 'seconds' must be a number, got {type(seconds).__name__}"
    if not math.isfinite(seconds):
        return f"Error: 'seconds' must be a finite number, got {seconds!r}"
    if seconds < 0:
        return "Error: seconds must be non-negative"
    if seconds > _MAX_SLEEP:
        return f"Error: sleep duration {seconds} exceeds maximum allowed ({_MAX_SLEEP} s)"
    try:
        time.sleep(seconds)
        return f"Slept for {seconds} seconds"
    except Exception as e:
        return f"Error: {str(e)}"


definition = {
    "type": "function",
    "function": {
        "name": "sleep",
        "description": "Sleep for a specified number of seconds. Useful for waiting between commands or polling for results.",
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Number of seconds to sleep.",
                },
            },
            "required": ["seconds"],
        },
    },
}
