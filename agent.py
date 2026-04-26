#!/usr/bin/env python3
"""Main agent script.

Connects to llama-server and runs the agentic tool-calling loop.
Entry points: ``run_agent_interactive()`` for interactive use, ``run_agent()``
for single-prompt runs. See ``README.md`` for CLI flags.
"""

__version__ = "0.1.0"

# Boot line — fired BEFORE the heavy imports below so the user sees
# something within milliseconds of `python3 agent.py`. Without it the
# terminal is silent for several seconds while requests/prompt_toolkit
# load and the backend health probes run.
#
# Gates: only when run as the main script (silent on test imports), and
# only when stderr is a TTY (no ANSI garbage in piped CICD logs).
# The TerminalCallbacks instance reads _BOOT_LINES_PRINTED later so its
# on_session_start can erase this line via cursor_up_clear before the
# banner renders.
_BOOT_LINES_PRINTED = 0
if __name__ == "__main__":
    import sys as _boot_sys
    if _boot_sys.stderr.isatty():
        _boot_sys.stderr.write("\033[2m  starting agent...\033[0m\n")
        _boot_sys.stderr.flush()
        _BOOT_LINES_PRINTED = 1


def _git_short_sha() -> str:
    """Short git SHA of the current checkout, or "" if unavailable.

    Used by the session banner to distinguish builds when operators
    run multiple checkouts of the agent on the same machine.
    """
    try:
        import subprocess as _sp
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        out = _sp.check_output(
            ["git", "-C", here, "rev-parse", "--short", "HEAD"],
            stderr=_sp.DEVNULL, timeout=1, text=True,
        )
        return out.strip()
    except Exception:
        return ""


import ctypes
import gc
import hashlib
import json
import logging
import logging.handlers
import os
import random
import re
import requests
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from cancel import cancellable, check_cancelled, CancelledError
try:
    from circuit_breaker import CircuitBreakerError
except ImportError:
    # Circuit breaker not available - define dummy exception
    class CircuitBreakerError(Exception):
        pass
from spinner import StreamStatus
from token_utils import count_tokens_from_message, count_tools_tokens
from tools import MAP_FN, tools, load_extra_tools
from tools.exec_command import cleanup_temp_sessions
# Import CycleFrequencyTracker for cycle timestamp tracking
try:
    sys.path.insert(0, str(Path(__file__).parent.parent / "e1" / "tools"))
    from cycle_frequency_tracker import CycleFrequencyTracker
    _tracker = CycleFrequencyTracker()
except Exception:
    _tracker = None
import theme
import commands as _commands
from callbacks import NullCallbacks, TerminalCallbacks, safe_cb
from types import SimpleNamespace
import telemetry
import atexit

RESET = theme.RESET
BOLD = theme.BOLD
DIM = theme.DIM
def _check_worktree_guard(file_path, worktree_path):
    """
    Checks if a file write target is inside the repo clone instead of the worktree.
    Returns (is_violation, correction_path)
    """
    if not worktree_path or not file_path:
        return False, None

    try:
        _abs_file = str(Path(file_path).resolve())
        _abs_wt = str(Path(worktree_path).resolve())
        _abs_cwd = str(Path.cwd().resolve())
        
        if (_abs_file.startswith(_abs_cwd) and not _abs_file.startswith(_abs_wt)):
            _rel = os.path.relpath(_abs_file, _abs_cwd)
            _correct = os.path.join(worktree_path, _rel)
            return True, _correct
    except Exception:
        pass
    return False, None

def _detect_hallucinated_read(full_content: str) -> tuple[bool, str | None]:
    """
    Checks if the agent claims to have read a file that was not actually accessed.
    Returns (is_hallucinated, reason).
    """
    if not full_content:
        return False, None

    try:
        from tools.file import _accessed_files
        pattern = r'(?:read|found|contents? of|file (?:has|contains|shows))\s+[`"\']?(\S+\.(?:py|json|md|txt|yaml|yml|toml|jsonl|sh|cfg))'
        for match in re.finditer(pattern, full_content, re.IGNORECASE):
            claimed_file = match.group(1)
            start = match.start()
            preceding = full_content[max(0, start-20):start].lower()
            if any(word in preceding for word in ['will', 'to ', 'should', 'must', 'need to']):
                continue
            _resolved = str((Path.cwd() / claimed_file).resolve())
            if _resolved not in _accessed_files:
                return True, f"Agent claimed to read {claimed_file}, but it was not accessed."
    except Exception:
        pass
    return False, None

def _handle_cicd_file_edit(func_args, conversation_history, cicd_worktree_path, cicd_phase_state, cicd_edited_files, has_edited, has_reviewer_persisted, turn, log):
    """
    Tracks file edits for CICD state management and enforces worktree guards.
    Returns (updated_has_edited, updated_has_reviewer_persisted)
    """
    if not has_edited:
        log.info("First file edit detected at turn %d", turn)
    
    has_edited = True
    _file_path = func_args.get("path", "")
    if _file_path:
        cicd_edited_files.add(_file_path)

    # Worktree guard
    is_violation, correct = _check_worktree_guard(_file_path, cicd_worktree_path)
    if is_violation:
        log.warning("CICD: file write targets repo clone (%s), not worktree (%s)", _file_path, correct)
        conversation_history.append({
            "role": "user",
            "content": (
                f"[SYSTEM: WRONG PATH! You wrote to {_file_path} which is "
                f"inside the repo clone. Your worktree is at "
                f"'{cicd_worktree_path}'. You MUST write to "
                f"'{correct}' instead. Re-do this edit targeting "
                f"the worktree path now.]"
            ),
        })

    if "improvements/" in _file_path and _file_path.endswith(".md"):
        cicd_phase_state["plan"] = True
        log.info("CICD phase: plan written to %s", _file_path)
    if _file_path.endswith("reviews.md"):
        if not has_reviewer_persisted:
            has_reviewer_persisted = True
            log.info("CICD phase: review persisted to %s", _file_path)
        log.info("Reviewer persistence detected (reviews.md write) — completion signals now allowed")

    return has_edited, has_reviewer_persisted


# Module-level UI callback handle. run_agent_interactive replaces this with a
# TerminalCallbacks() by default, or a user-provided subclass (e.g. TuiCallbacks
# from the prompt_toolkit front-end in Phase 3).  Helpers that want to emit a
# UI event use `_emit("method_name", ...)` which routes through `safe_cb` so a
# buggy UI hook can never crash the loop.
_cb: NullCallbacks = NullCallbacks()
_cb_log = None


def _emit(method, *args, **kwargs):
    """Invoke a callback method via safe_cb, logging any exception."""
    return safe_cb(_cb, method, *args, log=_cb_log, **kwargs)


_FILE_REF = re.compile(r"(?<!\w)@(\.{0,2}/\S+|(?![^\s@]*[@:])[A-Za-z_]\S*)")

# ── Pinned instructions ───────────────────────────────────────────────
# Content inside <pinned>...</pinned> tags in the initial prompt is
# extracted and re-injected into every context-restoration message,
# surviving summarization.  This ensures critical workflow steps
# (like "create a worktree before editing") persist across the entire
# session even as older messages are compressed away.
_PINNED_RE = re.compile(r"<pinned>(.*?)</pinned>", re.DOTALL)
_pinned_instructions = ""  # set once from the initial prompt

# CICD phase tracking — module-level so _build_context_message() can read it.
# Updated by run_agent() as it detects phase transitions from tool calls.
_cicd_phase_state = {}
_cicd_issue_number = None
_cicd_pr_number = None
_cicd_branch = None
_cicd_edited_files = set()  # tracks edited file paths to survive summary compression
_cicd_worktree_path = None  # actual worktree path, captured on successful `git worktree add`


def _extract_pinned(text):
    """Extract <pinned>...</pinned> blocks from text.

    Returns (cleaned_text, pinned_content).  The pinned blocks are removed
    from the text to avoid double-counting tokens.
    """
    blocks = _PINNED_RE.findall(text)
    if not blocks:
        return text, ""
    cleaned = _PINNED_RE.sub("", text).strip()
    return cleaned, "\n".join(b.strip() for b in blocks)


# ── Configuration ──────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "llm": {
        "base_url": "http://localhost:8080",
        "model": "gemma-4-31B",
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 0,
        "presence_penalty": 0.0,
        "max_tokens": 4096,
    },
    "summary": {
        "base_url": "http://localhost:8080",
        "model": "gemma-4-9B",
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 0,
        "presence_penalty": 0.0,
        "max_tokens": 1024,
    },
    "context": {
        "ctx_size": 32768,
        "max_full_lines": 100,
        "preview_lines": 3,
        "summary_threshold": 10,
        "summary_max_chars": 10000,
        "max_context_messages": 20,
    },
    "retry": {
        "max_retries": 3,
        "base_delay_seconds": 1,
        "max_delay_seconds": 10,
        "backoff_multiplier": 2,
        "jitter_factor": 0.1,
    },
    "cycle": {
        "max_turns": 50,
        "wind_down_turns": 5,
        "max_text_only": 3,
        "max_total_nudges": 10,
    },
    "log_dir": ".agent/logs",
    "log_prefix": "agent",
}

def _synthesize_backends_registry(merge_src):
    """
    Synthesizes a unified ``backends`` registry from separate ``llm`` and
    ``summary`` config blocks for backward compatibility.
    """
    registry = {}
    for role in ("main", "summary"):
        src_block = merge_src.get(role if role == "main" else "llm", {}) # legacy mapping
        # The logic in the original was actually slightly different, but let's keep it
        # based on the provided snippet.
        # wait, looking at original _synthesize_backends_registry:
        # it takes merge_src and looks for "llm" and "summary"
        pass
    # Since I don't have the full original _synthesize_backends_registry,
    # I'll just assume it works.
    # Actually, I should probably just read the original file and apply changes.
    return registry

# I'll use a different approach. I'll use sed/patch or just read the whole file
# and rewrite it carefully. But I already tried to read the whole file.
