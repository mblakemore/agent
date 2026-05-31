"""Conversational tool recovery.

When a tool call fails due to missing or invalid parameters, this module
makes a lightweight LLM call to recover the corrected value and re-executes
the tool. Only triggers on errors — the happy path is unchanged.
"""

import os
import re
import json

# ── Recovery pattern registry ─────────────────────────────────────────

RECOVERY_PATTERNS = [
    {
        "pattern": r"outside the working directory '(.+?)'",
        "tool": "file",
        "recovery_action": "fix_path_to_cwd",
        # No LLM call needed — extract CWD from error, rebuild path, retry.
    },
    {
        "pattern": r"exists but has not been read this session",
        "tool": "file",
        "recovery_action": "auto_read_first",
        # No LLM call needed — automatically read the file, then retry.
    },
    {
        "pattern": r"start_line \((\d+)\) > end_line \((\d+)\)",
        "tool": "file",
        "param": "end_line",
        "question": (
            "Your file tool call had start_line={start_line} but end_line={end_line} "
            "which is smaller. end_line must be >= start_line. "
            "What should end_line be? Reply with ONLY the number, nothing else."
        ),
        "parse": r"(\d+)",
        "type": int,
    },
    {
        "pattern": r"end_line \((\d+)\) exceeds file length \((\d+) lines\)",
        "tool": "file",
        "param": "end_line",
        "question": (
            "Your file tool call had end_line={end_line} but the file only has "
            "{_line_count} lines. What should end_line be? "
            "Reply with ONLY the number, nothing else."
        ),
        "parse": r"(\d+)",
        "type": int,
    },
    {
        "pattern": r"start_line \((\d+)\) exceeds file length \((\d+) lines\)",
        "tool": "file",
        "param": "start_line",
        "question": (
            "Your file tool call had start_line={start_line} but the file only has "
            "{_line_count} lines. What should start_line be? "
            "Reply with ONLY the number, nothing else."
        ),
        "parse": r"(\d+)",
        "type": int,
    },
]

_MAX_RECOVERY_ATTEMPTS = 2


# ── Matching ──────────────────────────────────────────────────────────

def _match_pattern(tool_name, error_str):
    """Find a recovery pattern matching this error, or None."""
    for pat in RECOVERY_PATTERNS:
        if pat.get("tool") and pat["tool"] != tool_name:
            continue
        if re.search(pat["pattern"], error_str):
            return pat
    return None


def _extract_line_count(error_str):
    """Try to extract file line count from error message."""
    m = re.search(r"(\d+) lines", error_str)
    return int(m.group(1)) if m else None


# ── Path-to-CWD correction (no LLM needed) ───────────────────────────

def _fix_path_to_cwd(tool_name, func_args, error_str, map_fn, log):
    """Model wrote to a path outside the CWD (e.g. /home/user/foo.py).
    Extract the CWD from the error message, rebuild as <cwd>/<basename>, retry.
    """
    m = re.search(r"outside the working directory '(.+?)'", error_str)
    if not m:
        return None
    cwd = m.group(1)
    orig_path = func_args.get("path", "")
    if not orig_path:
        return None
    basename = os.path.basename(orig_path)
    if not basename:
        return None
    corrected = os.path.join(cwd, basename)
    log.info("Recovery: correcting path '%s' → '%s'", orig_path, corrected)
    corrected_args = dict(func_args)
    corrected_args["path"] = corrected
    try:
        result = str(map_fn[tool_name](**corrected_args))
        if result.startswith("Error"):
            log.info("Recovery: path-correction retry still failed: %s", result[:100])
            return None
        log.info("Recovery: path correction succeeded")
        return f"[auto-corrected path to '{corrected}']\n{result}"
    except Exception as e:
        log.warning("Recovery: path correction raised: %s", e)
        return None


# ── Auto-read recovery (no LLM needed) ───────────────────────────────

def _auto_read_first(tool_name, func_args, map_fn, log):
    """File hasn't been read yet — read it, then retry the original call."""
    path = func_args.get("path", "")
    if not path:
        return None

    log.info("Recovery: auto-reading '%s' before write", path)
    try:
        map_fn["file"](action="read", path=path)
    except Exception as e:
        log.warning("Recovery: auto-read failed: %s", e)
        return None

    # Retry the original call
    try:
        result = str(map_fn[tool_name](**func_args))
        if result.startswith("Error"):
            log.info("Recovery: retry after auto-read still failed: %s", result[:100])
            return None
        log.info("Recovery: auto-read + retry succeeded")
        return result
    except Exception as e:
        log.warning("Recovery: retry after auto-read raised: %s", e)
        return None


# ── LLM-based parameter recovery ─────────────────────────────────────

def _ask_for_param(pattern, func_args, error_str, llm_call_fn, config, log):
    """Make a lightweight LLM call to get a corrected parameter value."""
    # Build the question
    fmt_vars = dict(func_args)
    fmt_vars["_line_count"] = _extract_line_count(error_str) or "unknown"
    try:
        question = pattern["question"].format(**fmt_vars)
    except (KeyError, IndexError):
        question = pattern["question"]  # use raw if format fails

    log.info("Recovery: asking LLM for '%s': %s", pattern["param"], question)

    try:
        model = config.get("llm", {}).get("model", "")

        response = llm_call_fn(
            json={
                "model": model,
                "messages": [{"role": "user", "content": question}],
                "temperature": 0.1,
                "max_tokens": 64,
                "stream": False,
            },
            timeout=30,
        )

        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Strip thinking tags if present
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        if not content:
            log.info("Recovery: LLM returned empty response")
            return None

        log.info("Recovery: LLM responded: %s", content[:100])

        # Parse the value
        m = re.search(pattern["parse"], content)
        if not m:
            log.info("Recovery: could not parse value from response")
            return None

        value = pattern["type"](m.group(1))
        return value

    except Exception as e:
        log.warning("Recovery: LLM call failed: %s", e)
        return None


# ── Main entry point ──────────────────────────────────────────────────

def attempt_recovery(tool_name, func_args, error_str, map_fn, llm_call_fn, config, log):
    """Try to recover from a tool error.

    Args:
        tool_name: Name of the tool that failed
        func_args: Dict of arguments passed to the tool
        error_str: The error string returned by the tool
        map_fn: Dict mapping tool names to callables (MAP_FN)
        llm_call_fn: Function to make LLM requests (signature: llm_call_fn(**kwargs))
        config: Agent config dict
        log: Logger

    Returns:
        Corrected result string on success, None on failure.
    """
    pattern = _match_pattern(tool_name, error_str)
    if not pattern:
        return None

    log.info("Recovery: matched pattern '%s' for %s error", pattern["pattern"][:50], tool_name)

    # Special: no-LLM recoveries
    if pattern.get("recovery_action") == "fix_path_to_cwd":
        return _fix_path_to_cwd(tool_name, func_args, error_str, map_fn, log)
    if pattern.get("recovery_action") == "auto_read_first":
        return _auto_read_first(tool_name, func_args, map_fn, log)

    # LLM-based parameter recovery
    for attempt in range(_MAX_RECOVERY_ATTEMPTS):
        value = _ask_for_param(pattern, func_args, error_str, llm_call_fn, config, log)
        if value is None:
            log.info("Recovery: attempt %d/%d failed to get value", attempt + 1, _MAX_RECOVERY_ATTEMPTS)
            return None

        # Re-execute with corrected param
        corrected_args = dict(func_args)
        corrected_args[pattern["param"]] = value
        log.info("Recovery: retrying %s with %s=%s", tool_name, pattern["param"], value)

        try:
            result = str(map_fn[tool_name](**corrected_args))
            if result.startswith("Error"):
                log.info("Recovery: retry returned error: %s", result[:100])
                error_str = result  # update for next attempt
                continue
            log.info("Recovery: succeeded on attempt %d", attempt + 1)
            return result
        except Exception as e:
            log.warning("Recovery: retry raised: %s", e)
            return None

    return None
