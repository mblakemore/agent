#!/usr/bin/env python3
"""
Agent script with file reading/writing tools.
Connects to llama-server and executes tool calls in an agentic loop.

SHARED RUNTIME — DO NOT MODIFY. This file is part of tool-agent/ and is used by all agents.
Do NOT create symlinks to this directory. Do NOT add __init__.py files here.
Do NOT change import statements. If you need to extend functionality, create
tools in your own tools/ directory instead.
"""

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
from token_utils import count_tokens_from_message, count_tools_tokens, _QWEN_TOKENIZER_AVAILABLE
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
import callbacks as _cbmod
import commands as _commands
from callbacks import NullCallbacks, TerminalCallbacks, safe_cb
from types import SimpleNamespace

RESET = theme.RESET
BOLD = theme.BOLD
DIM = theme.DIM

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


_FILE_REF = re.compile(r"@(\S+)")

# ── Configuration ──────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "llm": {"base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B"},
    "retry": {
        "max_retries": 10,
        "base_delay_seconds": 2,
        "max_delay_seconds": 60,
        "backoff_multiplier": 2.0,
        "jitter_factor": 0.1,
    },
    "context": {
        "max_full_lines": 800,
        "preview_lines": 200,
        "summary_threshold": 5,
        "summary_max_chars": 3000,
        "max_context_messages": 30,
        "ctx_size": 114688,
        "max_tokens": 16384,
    },
    "cycle": {
        "max_turns": 100,
        "wind_down_turns": 10,
    },
    "generation": {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
        "presence_penalty": 0.0,
    },
    "summary": {
        "base_url": "http://127.0.0.1:8082",
        "model": "gemma-4-E4B",
        "enabled": True,
        "max_wait_on_save": 10,
    },
}


def _load_config():
    """Load configuration from CWD/config.json, deep-merged with defaults."""
    config = json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy

    config_path = Path(os.getcwd()) / "config.json"
    if not config_path.exists():
        return config

    try:
        with open(config_path, "r") as f:
            user_config = json.load(f)
        for section in config:
            if section in user_config and isinstance(user_config[section], dict):
                config[section].update(user_config[section])
        return config
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not load config.json, using defaults: {e}")
        return config


_config = _load_config()

# Apply configuration
BASE_URL = _config["llm"]["base_url"]
_MAX_FULL_LINES = _config["context"]["max_full_lines"]
_PREVIEW_LINES = _config["context"]["preview_lines"]
_SUMMARY_THRESHOLD = _config["context"]["summary_threshold"]
_SUMMARY_MAX_CHARS = _config["context"].get("summary_max_chars", 1500)
_MAX_CONTEXT_MESSAGES = _config["context"]["max_context_messages"]

_LLM_MAX_RETRIES = _config["retry"]["max_retries"]
_LLM_BASE_DELAY = _config["retry"]["base_delay_seconds"]
_LLM_MAX_DELAY = _config["retry"]["max_delay_seconds"]
_LLM_BACKOFF_MULTIPLIER = _config["retry"]["backoff_multiplier"]
_LLM_JITTER_FACTOR = _config["retry"]["jitter_factor"]

_MAX_TURNS = _config["cycle"]["max_turns"]
_WIND_DOWN_TURNS = _config["cycle"]["wind_down_turns"]

# Auto-nudge on text-only responses. Off by default; enable with --nudge.
_NUDGE_ENABLED = False

# Load agent-specific tools from CWD/tools/ if it exists
_agent_tools_dir = os.path.join(os.getcwd(), "tools")
if os.path.isdir(_agent_tools_dir):
    load_extra_tools(_agent_tools_dir)


# ── Retry logic ────────────────────────────────────────────────────────

def _calculate_retry_delay(attempt):
    """Calculate retry delay with exponential backoff and jitter."""
    delay = _LLM_BASE_DELAY * (_LLM_BACKOFF_MULTIPLIER ** attempt)
    delay = min(delay, _LLM_MAX_DELAY)
    if _LLM_JITTER_FACTOR > 0:
        jitter_range = delay * _LLM_JITTER_FACTOR
        delay = delay + random.uniform(-jitter_range, jitter_range)
        delay = max(0, delay)
    return round(delay, 2)


class ContextOverflowError(Exception):
    """Raised when the server returns persistent 500s likely due to context overflow."""
    pass


def _llm_request(log, **kwargs):
    """POST to the LLM with retries and exponential backoff.

    Raises ContextOverflowError after 3 consecutive 500s (likely context overflow).
    Other transient errors (503, connection) retry up to _LLM_MAX_RETRIES.
    """
    consecutive_500s = 0
    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            response = requests.post(f"{BASE_URL}/v1/chat/completions", **kwargs)
            if response.status_code >= 500:
                if response.status_code == 500:
                    consecutive_500s += 1
                    if consecutive_500s >= 3:
                        raise ContextOverflowError(
                            f"3 consecutive HTTP 500 errors — likely context overflow")
                else:
                    consecutive_500s = 0
                raise requests.exceptions.HTTPError(
                    f"Server error {response.status_code}", response=response)
            response.raise_for_status()
            return response
        except ContextOverflowError:
            raise  # Don't retry — caller will reduce context
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError) as e:
            if attempt == _LLM_MAX_RETRIES:
                raise
            if hasattr(e, 'response') and e.response is not None and e.response.status_code < 500:
                raise
            delay = _calculate_retry_delay(attempt)
            log.warning("LLM request failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, _LLM_MAX_RETRIES + 1, e, delay)
            _emit("on_api_retry", str(e), attempt + 1, _LLM_MAX_RETRIES, delay)
            time.sleep(delay)


# ── Text utilities ─────────────────────────────────────────────────────

_UNICODE_MAP = str.maketrans({
    "\u2014": "--", "\u2013": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2022": "*",
    "\u00a0": " ", "\u200b": "",
})


_THINK_TAG_RE = re.compile(r'</?think>|<\|channel>thought\n.*?<channel\|>', re.DOTALL)


def _sanitize(text):
    """Replace common Unicode characters with ASCII equivalents and strip think tags."""
    text = _THINK_TAG_RE.sub('', text)
    return text.translate(_UNICODE_MAP)


def _sanitize_display(text):
    """Unicode replacement only — think tags are handled by _ReasoningRenderer."""
    return text.translate(_UNICODE_MAP)


class _ReasoningRenderer:
    """Stream-aware renderer that wraps <think>…</think> blocks in a
    violet [Reasoning] header + dim body, emitting everything else normally.

    Handles tags split across delta chunks via a small pending buffer.
    The `writer` is a callable taking a pre-styled text chunk — in practice
    this is `lambda t: _emit("on_stream_chunk", t)` so the UI callback
    layer gets every printable chunk.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    _MAX_PENDING = max(len(_OPEN), len(_CLOSE)) - 1  # 7

    def __init__(self, writer):
        self._write = writer
        self._pending = ""
        self._in_think = False

    def feed(self, chunk):
        buf = self._pending + chunk
        self._pending = ""
        while buf:
            if self._in_think:
                close_idx = buf.find(self._CLOSE)
                if close_idx == -1:
                    # Keep last few chars in case the close tag is splitting
                    if len(buf) > self._MAX_PENDING:
                        self._emit_think(buf[:-self._MAX_PENDING])
                        self._pending = buf[-self._MAX_PENDING:]
                    else:
                        self._pending = buf
                    return
                self._emit_think(buf[:close_idx])
                buf = buf[close_idx + len(self._CLOSE):]
                self._close_block()
            else:
                open_idx = buf.find(self._OPEN)
                if open_idx == -1:
                    if len(buf) > self._MAX_PENDING:
                        self._emit_plain(buf[:-self._MAX_PENDING])
                        self._pending = buf[-self._MAX_PENDING:]
                    else:
                        self._pending = buf
                    return
                self._emit_plain(buf[:open_idx])
                buf = buf[open_idx + len(self._OPEN):]
                self._open_block()

    def flush(self):
        if self._pending:
            if self._in_think:
                self._emit_think(self._pending)
            else:
                self._emit_plain(self._pending)
            self._pending = ""
        if self._in_think:
            self._close_block()

    def _emit_plain(self, text):
        if not text:
            return
        self._write(_sanitize_display(text))

    def _emit_think(self, text):
        if not text:
            return
        self._write(theme.dim(_sanitize_display(text)))

    def _open_block(self):
        self._in_think = True
        self._write(theme.c(theme.VIOLET, "\n[Reasoning]\n", bold=True))

    def _close_block(self):
        self._in_think = False
        self._write(theme.c(theme.VIOLET, "\n[/Reasoning]\n", bold=True))


_FILE_ACTIONS = {"read", "write", "insert", "append", "delete", "list"}


def _sanitize_tool_args(func_name, args, log):
    """Fix garbled args that parsed as valid JSON but have bogus values.

    Gemma 4 concatenates **,key:value into field values, e.g.:
      {"action": "write**,content:some text"}
      {"action": "write", "path": "foo.json**,start_line:1", "end_line": 14}
    This extracts embedded params from ALL string fields.
    """
    if func_name != "file" or not isinstance(args, dict):
        return args

    # Check if any string value contains the **,key: pattern
    _GARBLE_PAT = re.compile(r'\*\*,(\w+):')
    needs_fix = False
    for v in args.values():
        if isinstance(v, str) and _GARBLE_PAT.search(v):
            needs_fix = True
            break

    action = args.get("action", "")
    if not needs_fix and action in _FILE_ACTIONS:
        return args

    log.warning("Sanitizing garbled file args: %s",
                {k: repr(v)[:60] for k, v in args.items()})

    # Collect all key:value pairs from garbled strings across all fields
    extracted = {}
    clean_vals = {}
    for key, val in args.items():
        if not isinstance(val, str):
            extracted[key] = val
            continue
        # Split on **,key: boundaries to extract embedded params
        parts = _GARBLE_PAT.split(val)
        # parts[0] is the clean prefix of this field's value
        clean_val = parts[0].rstrip('*').strip()
        if clean_val:
            clean_vals[key] = clean_val
        # Remaining parts alternate: key_name, value_before_next_split
        for i in range(1, len(parts) - 1, 2):
            embed_key = parts[i]
            embed_val = parts[i + 1].rstrip('*').strip() if i + 1 < len(parts) else ""
            if embed_val:
                # Try to parse integers for line numbers
                if embed_key in ("start_line", "end_line"):
                    try:
                        extracted[embed_key] = int(embed_val)
                    except ValueError:
                        extracted[embed_key] = embed_val
                else:
                    extracted[embed_key] = embed_val

    # Build fixed args: extracted embedded params + clean field values
    fixed = {}
    for key, val in clean_vals.items():
        fixed[key] = val
    for key, val in extracted.items():
        if key not in fixed:
            fixed[key] = val

    # Fix action if it was garbled
    if "action" in fixed and fixed["action"] not in _FILE_ACTIONS:
        for valid_action in _FILE_ACTIONS:
            if valid_action in str(fixed["action"]).lower():
                fixed["action"] = valid_action
                break

    log.info("Sanitized args: %s",
             {k: repr(v)[:60] if isinstance(v, str) else v for k, v in fixed.items()})
    return fixed


def _salvage_tool_args(func_name, raw_args, log):
    """Try to extract valid arguments from garbled Gemma 4 tool call output.

    Gemma 4 sometimes concatenates keys into the action field
    (e.g. "write**,content:..." or "read,path:..."). This attempts to
    recover the intended arguments.

    Returns a dict on success, None if unsalvageable.
    """
    try:
        # Strip Gemma 4 special tokens that leak into args
        cleaned = raw_args.replace('<|"|>', '"').replace('<|', '').replace('|>', '')
        # Try parsing again after cleanup
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Pattern: "action**,key:value,key:value" or "action,key:value"
        # Try to extract action and rebuild as JSON
        if func_name == "file":
            for action in ("read", "write", "insert", "append", "delete", "list"):
                if action in raw_args.lower():
                    result = {"action": action}
                    # Try to find path
                    path_match = re.search(r'path["\s:]+([^\s,}"]+)', raw_args)
                    if path_match:
                        result["path"] = path_match.group(1).strip('"\'')
                    # Try to find content
                    content_match = re.search(r'content["\s:]+(.+?)(?:,\s*(?:path|start_line|end_line)|$)', raw_args, re.DOTALL)
                    if content_match:
                        result["content"] = content_match.group(1).strip('"\'')
                    if "path" in result:
                        log.warning("Salvaged garbled tool args: %s → %s", raw_args[:100], result)
                        return result

        # For exec_command, try to find the command string
        if func_name == "exec_command":
            cmd_match = re.search(r'command["\s:]+(.+)', raw_args, re.DOTALL)
            if cmd_match:
                cmd = cmd_match.group(1).strip('"\'').rstrip('}')
                log.warning("Salvaged garbled exec_command: %s", cmd[:100])
                return {"command": cmd}

    except Exception as e:
        log.debug("Salvage attempt failed: %s", e)

    return None


# ── Token estimation ───────────────────────────────────────────────────

def _estimate_tokens(msg):
    return count_tokens_from_message(msg)


_TOOLS_TOKENS = None


def _estimate_tools_tokens():
    global _TOOLS_TOKENS
    if _TOOLS_TOKENS is None:
        _TOOLS_TOKENS = count_tools_tokens(tools)
    return _TOOLS_TOKENS


# ── File reference expansion ──────────────────────────────────────────

def _expand_file_refs(text):
    """Expand @filepath references in user input to inline file contents.

    Returns (expanded_text, files_content, error).
    """
    refs = _FILE_REF.findall(text)
    if not refs:
        return text, None, None

    seen = set()
    attachments = []
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)

        p = Path(ref)
        if not p.exists():
            return None, None, f"Error: file '{ref}' does not exist"
        if p.is_dir():
            return None, None, f"Error: '{ref}' is a directory, not a file"

        lines = p.read_text().splitlines(True)
        total = len(lines)
        if total <= _MAX_FULL_LINES or p.name == "agent.md":
            content = "".join(lines)
            header = f"[{ref}: {total} lines]"
        else:
            content = "".join(lines[:_PREVIEW_LINES])
            header = f"[{ref}: first {_PREVIEW_LINES} of {total} lines]"

        resolved = str(p.resolve())
        if p.name == "agent.md":
            header = (f"[AGENT IDENTITY FILE: {ref} (loaded from {resolved}). "
                      f"This is YOUR agent.md — do not search for it elsewhere. {total} lines]")

        attachments.append(f"{header}\n{content}")
        _emit("on_file_attached", header)

    files_content = "\n\n".join(attachments)
    # Prepend working directory context when agent.md is loaded
    cwd = os.getcwd()
    if any(Path(ref).name == "agent.md" for ref in seen):
        preamble = (
            f"[SYSTEM CONTEXT: Your working directory is {cwd}. "
            f"All relative paths resolve from here. "
            f"Do not cd to other repositories or search for files outside this tree.]\n\n"
        )
    else:
        preamble = ""
    expanded = text + "\n\n" + preamble + files_content
    return expanded, preamble + files_content if preamble else files_content, None


# ── Summarization ─────────────────────────────────────────────────────

def _format_for_summary(messages):
    """Format messages into a readable transcript for the summarizer.

    Preserves error messages and tool results more fully than regular content,
    since errors are critical for avoiding repeated failed approaches.
    """
    parts = []
    for m in messages:
        role = m["role"].upper()
        if role == "TOOL":
            name = m.get("name", "?")
            content = m.get("content", "")
            # Preserve error messages fully (up to 800 chars) — they're critical context
            is_error = content.startswith("Error") or "Error:" in content[:50]
            max_len = 800 if is_error else 500
            if len(content) > max_len:
                content = content[:max_len] + "..."
            parts.append(f"TOOL RESULT ({name}): {content}")
        elif role == "ASSISTANT":
            text = m.get("content", "")
            tool_calls = m.get("tool_calls", [])
            if text:
                if len(text) > 600:
                    text = text[:600] + "..."
                parts.append(f"ASSISTANT: {text}")
            for tc in tool_calls:
                fn_info = tc.get("function", {})
                args = fn_info.get("arguments", "")
                if len(args) > 200:
                    args = args[:200] + "..."
                parts.append(f"ASSISTANT called {fn_info.get('name', '?')}({args})")
        else:
            content = m.get("content", "")
            if len(content) > 800:
                content = content[:800] + "..."
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _summary_request(prompt, log, base_url=None, model=None):
    """POST a summary prompt to the given endpoint. Returns summary text.

    Args:
        base_url: Override endpoint (e.g. CPU model on port 8082).
                  Defaults to the summary config, then the main model.
        model:    Override model name. Defaults to summary config, then main.
    """
    summary_cfg = _config.get("summary", {})
    url = base_url or summary_cfg.get("base_url") or BASE_URL
    mdl = model or summary_cfg.get("model") or _config["llm"]["model"]

    request_body = {
        "model": mdl,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "top_p": 0.9,
        "top_k": 20,
        "presence_penalty": 0.0,
        "max_tokens": 1024,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }

    response = requests.post(f"{url}/v1/chat/completions",
                             json=request_body, timeout=120)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def _condense_summary(text, log=None):
    """Re-summarize if text exceeds the character cap. Preserves all info."""
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    if log:
        log.info("Summary too long (%d chars, limit %d) — condensing", len(text), _SUMMARY_MAX_CHARS)
    _emit("on_summary_start", 0)
    prompt = (
        f"The following summary is too long ({len(text)} chars). "
        f"Rewrite it in under {_SUMMARY_MAX_CHARS // 2} characters. "
        f"Keep ALL file paths, decisions, failures, and completed actions. "
        f"Remove filler and verbose descriptions. Be maximally terse.\n\n"
        f"{text}"
    )
    try:
        condensed = _summary_request(prompt, log)
        if len(condensed) > _SUMMARY_MAX_CHARS:
            # Model didn't comply — hard truncate as last resort
            if log:
                log.warning("Condensed summary still too long (%d chars), truncating", len(condensed))
            condensed = condensed[:_SUMMARY_MAX_CHARS].rsplit('\n', 1)[0] + "\n[...truncated]"
        if log:
            log.info("Condensed summary: %d → %d chars", len(text), len(condensed))
        _emit("on_notice", "info", f"[summary condensed: {len(text)} → {len(condensed)} chars]")
        return condensed
    except Exception as e:
        if log:
            log.error("Condense failed (%s), truncating as fallback", e)
        return text[:_SUMMARY_MAX_CHARS].rsplit('\n', 1)[0] + "\n[...truncated]"


def _build_summary_prompt(old_summary, new_messages):
    """Build the summary prompt from old summary + new messages."""
    transcript = _format_for_summary(new_messages)

    structure_instruction = (
        "Write a CONCISE summary (under 400 words) with these sections:\n"
        "GOAL: One line — the objective.\n"
        "DONE: Bullet list of completed actions with file paths. Critical — prevents re-doing work.\n"
        "FAILED: Approaches that failed and why (one line each).\n"
        "STATE: Current state in 1-2 sentences.\n"
        "NEXT: The single next action.\n"
        "Be terse. Use file paths, not descriptions. No filler."
    )

    if old_summary:
        return (
            f"Here is the previous summary of the conversation so far:\n\n"
            f"{old_summary}\n\n"
            f"Here are the new messages since that summary:\n\n"
            f"{transcript}\n\n"
            f"Write an updated summary that combines the previous summary with the new messages.\n\n"
            f"{structure_instruction}"
        )
    return (
        f"Here is a conversation transcript:\n\n"
        f"{transcript}\n\n"
        f"Write a concise summary.\n\n"
        f"{structure_instruction}"
    )


def _generate_summary(old_summary, new_messages, log):
    """Call the LLM to produce an updated conversation summary.

    The summary prompt explicitly preserves decisions, outcomes, and failed
    approaches to prevent the agent from repeating mistakes.

    Tries the dedicated summary endpoint first (CPU model on port 8082),
    falls back to the main model on connection failure.
    """
    prompt = _build_summary_prompt(old_summary, new_messages)
    log.info("Generating conversation summary...")

    summary_cfg = _config.get("summary", {})
    summary_url = summary_cfg.get("base_url")
    try:
        # Try dedicated summary endpoint first
        if summary_cfg.get("enabled") and summary_url:
            summary = _summary_request(prompt, log)
        else:
            summary = _summary_request(prompt, log, base_url=BASE_URL,
                                       model=_config["llm"]["model"])
        log.info("SUMMARY: %s", summary)
        return summary
    except (requests.ConnectionError, requests.Timeout) as e:
        if summary_url and summary_url != BASE_URL:
            log.warning("Summary endpoint unavailable (%s), falling back to main model", e)
            try:
                summary = _summary_request(prompt, log, base_url=BASE_URL,
                                           model=_config["llm"]["model"])
                log.info("SUMMARY (fallback): %s", summary)
                return summary
            except Exception as e2:
                log.error("Summary fallback also failed: %s", e2)
                return old_summary or ""
        log.error("Summary generation failed: %s", e)
        return old_summary or ""
    except Exception as e:
        log.error("Summary generation failed: %s", e)
        return old_summary or ""


# ── Async summarizer ──────────────────────────────────────────────────

class AsyncSummarizer:
    """Background-thread summarizer targeting a separate (CPU) model endpoint.

    Usage:
        summarizer.kick(old_text, messages, up_to_idx)  # non-blocking
        if summarizer.harvest(summary_state):            # pick up result
            ...
        summarizer.drain()                               # block before checkpoint
    """

    def __init__(self, config, log):
        self._config = config
        self._log = log
        self._lock = threading.Lock()
        self._thread = None
        self._pending_result = None
        self._pending_up_to = None
        self._running = False

    def kick(self, old_summary_text, messages_snapshot, up_to_idx):
        """Start background summarization if not already running."""
        with self._lock:
            if self._running:
                return
            self._running = True

        msgs = deepcopy(messages_snapshot)

        def _worker():
            try:
                prompt = _build_summary_prompt(old_summary_text, msgs)
                summary_cfg = self._config.get("summary", {})
                # Try dedicated endpoint, fall back to main model
                try:
                    result = _summary_request(prompt, self._log)
                except (requests.ConnectionError, requests.Timeout) as e:
                    self._log.warning("Async summary endpoint unavailable (%s), "
                                      "falling back to main model", e)
                    result = _summary_request(
                        prompt, self._log,
                        base_url=BASE_URL,
                        model=self._config["llm"]["model"],
                    )
                self._log.info("ASYNC SUMMARY: %s", result)
                with self._lock:
                    self._pending_result = result
                    self._pending_up_to = up_to_idx
            except Exception as e:
                self._log.error("Async summary failed: %s", e)
            finally:
                with self._lock:
                    self._running = False

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

    def harvest(self, summary_state):
        """If a completed summary is available, apply it. Returns True if updated."""
        with self._lock:
            if self._pending_result is None:
                return False
            summary_state["text"] = _condense_summary(self._pending_result, self._log)
            summary_state["up_to"] = self._pending_up_to
            self._pending_result = None
            self._pending_up_to = None
            return True

    @property
    def is_running(self):
        with self._lock:
            return self._running

    def drain(self, timeout=None):
        """Block until pending summary completes (for checkpoint saves)."""
        if timeout is None:
            timeout = self._config.get("summary", {}).get("max_wait_on_save", 10)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def reset(self):
        """Discard any pending result (for /clear)."""
        with self._lock:
            self._pending_result = None
            self._pending_up_to = None


# ── Context window management ─────────────────────────────────────────

def _build_context(conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
                    max_messages_override=None):
    """Build the context window dynamically based on token budget.

    When messages are dropped and a summary exists, a synthetic user message
    is prepended containing the initial file contents and the progress summary.

    Args:
        max_messages_override: If set, cap messages to this count (used for
            context reduction on 500 errors).

    Returns (messages_to_send, oldest_included_idx).
    """
    global _TOOLS_TOKENS
    if _TOOLS_TOKENS is None:
        _TOOLS_TOKENS = _estimate_tools_tokens()

    reserved_output = min(max_tokens, ctx_size // 2)
    # Safety margin: ctx_size//4 gives ~25% headroom.  This accounts for
    # chat-template overhead (~4 special tokens per message boundary, system
    # prompt formatting for tools) that our tokenizer doesn't count.
    budget = ctx_size - _TOOLS_TOKENS - reserved_output - max(512, ctx_size // 4)
    effective_max = max_messages_override if max_messages_override else _MAX_CONTEXT_MESSAGES

    context_msg = None
    context_tokens = 0
    if summary_state["text"]:
        parts = []
        if initial_files:
            parts.append(initial_files)
        parts.append(f"Progress summary of work done so far:\n{summary_state['text']}")
        parts.append(
            f"IMPORTANT: Your working directory is '{os.getcwd()}'. "
            "Use relative paths (e.g. '.agent/state/file.json') — do not cd elsewhere. "
            "Continue where you left off. Do not repeat already-completed steps. "
            "TOOL RULE: To write JSON files, use exec_command with heredoc "
            "(e.g. cat > file.json << 'EOF'\\n...\\nEOF). "
            "Do NOT use the file tool with action='write' for JSON content."
        )
        context_msg = {"role": "user", "content": "\n\n".join(parts)}
        context_tokens = _estimate_tokens(context_msg)

        # If summary takes a large share of the budget, reduce message count
        # rather than truncating the summary — the summary IS the agent's memory
        # of all prior work and must be preserved intact.
        if context_tokens > budget * 0.8:
            # Summary alone exceeds the budget — condense it
            log.warning("Summary exceeds 80%% of budget (%d/%d tokens) — condensing", context_tokens, budget)
            summary_state["text"] = _condense_summary(summary_state["text"], log)
            # Rebuild context_msg with condensed summary
            parts = []
            if initial_files:
                parts.append(initial_files)
            parts.append(f"Progress summary of work done so far:\n{summary_state['text']}")
            parts.append(
                f"IMPORTANT: Your working directory is '{os.getcwd()}'. "
                "Use relative paths (e.g. '.agent/state/file.json') — do not cd elsewhere. "
                "Continue where you left off. Do not repeat already-completed steps."
            )
            context_msg = {"role": "user", "content": "\n\n".join(parts)}
            context_tokens = _estimate_tokens(context_msg)
            log.info("Condensed summary, context now %d tokens", context_tokens)
        if context_tokens > budget * 0.5:
            # Summary is large — reduce message count to make room
            remaining_budget = budget - context_tokens
            avg_msg_tokens = 150  # rough estimate per message
            max_msgs = max(2, int(remaining_budget / avg_msg_tokens))
            if max_msgs < effective_max:
                log.info("Summary uses %d/%d tokens — reducing messages from %d to %d",
                         context_tokens, budget, effective_max, max_msgs)
                effective_max = max_msgs

    used = 0
    selected = []
    oldest_idx = len(conversation_history)
    for i in range(len(conversation_history) - 1, -1, -1):
        if len(selected) >= effective_max:
            break
        msg_tokens = _estimate_tokens(conversation_history[i])
        needed = used + msg_tokens
        if context_msg and i > 0:
            needed += context_tokens
        if needed > budget:
            break
        selected.append(conversation_history[i])
        used += msg_tokens
        oldest_idx = i

    selected.reverse()

    if oldest_idx > 0 and context_msg:
        selected.insert(0, context_msg)
        used += context_tokens
    elif not any(m["role"] == "user" for m in selected):
        if initial_files:
            selected.insert(0, {"role": "user", "content": initial_files + "\n\nContinue."})
        else:
            for i in range(oldest_idx - 1, -1, -1):
                if conversation_history[i]["role"] == "user":
                    selected.insert(0, conversation_history[i])
                    break

    log.debug("Context budget: %d/%d tokens, %d messages, oldest_idx=%d, has_context_msg=%s",
              used, budget, len(selected), oldest_idx, context_msg is not None)
    return selected, oldest_idx


def _maybe_resummarize(conversation_history, summary_state, oldest_idx, log, force=False):
    """Check if enough messages have fallen out of the window to warrant a new summary."""
    unsummarized = oldest_idx - summary_state["up_to"]

    if not force and unsummarized < _SUMMARY_THRESHOLD:
        return False

    new_messages = conversation_history[summary_state["up_to"]:oldest_idx]

    # Nothing new to summarize — keep existing summary intact
    if not new_messages:
        log.debug("Resummarize skipped: 0 new messages (up_to=%d, oldest_idx=%d)",
                  summary_state["up_to"], oldest_idx)
        return False

    _emit("on_summary_start", len(new_messages))
    summary = _generate_summary(summary_state["text"], new_messages, log)
    summary_state["text"] = _condense_summary(summary, log)
    summary_state["up_to"] = oldest_idx
    _emit("on_summary_done")
    return True


# ── Logger setup ──────────────────────────────────────────────────────

def _setup_logger():
    """Create a structured logger with levels, rotation, and console output."""
    log_dir_override = _config.get("log_dir")
    if log_dir_override:
        history_dir = os.path.join(os.getcwd(), log_dir_override)
    else:
        history_dir = _HISTORY_DIR
    os.makedirs(history_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_prefix = _config.get("log_prefix", "session")
    log_path = os.path.join(history_dir, f"{log_prefix}_{timestamp}.log")
    error_log_path = os.path.join(history_dir, "errors.log")

    logger = logging.getLogger("agent")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        error_log_path, maxBytes=5*1024*1024, backupCount=3)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter('%(asctime)s ERROR %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(error_handler)

    return logger, log_path, error_log_path


# ── Conversation checkpoints (for -c continue) ──────────────────────

_AGENT_DIR = os.path.join(os.getcwd(), ".agent")
_STATE_DIR = os.path.join(_AGENT_DIR, "state")
_HISTORY_DIR = os.path.join(_AGENT_DIR, "history")


def _state_path(*parts):
    """Return a path inside .agent/state under the current working directory."""
    return os.path.join(_STATE_DIR, *parts)


def _ensure_agent_dirs():
    """Create .agent/state and .agent/history on first use."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    os.makedirs(_HISTORY_DIR, exist_ok=True)


_ensure_agent_dirs()
_CHECKPOINT_PATH = _state_path("conversation_checkpoint.json")


def _strip_checkpoint_reads(conversation_history):
    """Remove tool results that contain conversation_checkpoint.json content.

    Prevents recursive self-inclusion: when the agent reads its own checkpoint
    file, the full conversation history gets embedded in the tool result, causing
    exponential growth on each subsequent checkpoint save.
    """
    cleaned = []
    for msg in conversation_history:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and "conversation_checkpoint.json" in content and len(content) > 10_000:
                msg = dict(msg, content="[conversation_checkpoint.json content stripped — internal runtime file]")
        cleaned.append(msg)
    return cleaned


def _save_checkpoint(conversation_history, summary_state, turn, initial_files):
    """Save conversation state so a crashed cycle can be resumed with -c."""
    try:
        checkpoint = {
            "conversation_history": _strip_checkpoint_reads(conversation_history),
            "summary_state": summary_state,
            "turn": turn,
            "initial_files": initial_files,
        }
        os.makedirs(os.path.dirname(_CHECKPOINT_PATH), exist_ok=True)
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump(checkpoint, f)
    except Exception:
        pass  # best-effort, don't crash the agent


def _load_checkpoint():
    """Load a saved conversation checkpoint. Returns (history, summary, turn, files) or None."""
    if not os.path.exists(_CHECKPOINT_PATH):
        return None
    try:
        with open(_CHECKPOINT_PATH) as f:
            cp = json.load(f)
        return (
            cp["conversation_history"],
            cp["summary_state"],
            cp.get("turn", 0),
            cp.get("initial_files"),
        )
    except Exception:
        return None


def _delete_checkpoint():
    """Remove checkpoint after a clean exit."""
    try:
        if os.path.exists(_CHECKPOINT_PATH):
            os.remove(_CHECKPOINT_PATH)
    except Exception:
        pass


# ── Cycle auto-increment ─────────────────────────────────────────────

def _auto_increment_cycle(log):
    """Check if the current cycle was already committed and bump if so.

    Compares the cycle number in .agent/state/current-state.json against the
    latest git log entries.  If a 'C<N>:' commit exists for the current
    cycle, the state file is incremented to N+1 so the agent starts the
    next cycle instead of repeating.

    Safety: only bumps if the state file is clean (matches the last commit).
    If the file has uncommitted changes, a previous session may have already
    bumped it — touching it again would cause a double-increment/skip.
    """
    state_path = _state_path("current-state.json")
    if not os.path.exists(state_path):
        return

    try:
        with open(state_path) as f:
            state = json.load(f)
        cycle = int(state.get("cycle", 0))
        if cycle <= 0:
            return

        # Check recent git log for committed cycles
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return

        # Find the highest committed cycle number from 'C<N>:' patterns
        import re as _re
        committed_cycles = set()
        for line in result.stdout.strip().split("\n"):
            m = _re.search(r'\bC(\d+):', line)
            if m:
                committed_cycles.add(int(m.group(1)))

        if not committed_cycles:
            return

        highest_committed = max(committed_cycles)

        # Only bump if current cycle has been committed (or is behind)
        if cycle <= highest_committed:
            new_cycle = highest_committed + 1
            state["cycle"] = new_cycle
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2)
                f.write("\n")

            # Also bump focus.json if it exists and matches old cycle
            focus_path = os.path.join(os.getcwd(), "state", "focus.json")
            if os.path.exists(focus_path):
                try:
                    with open(focus_path) as f:
                        focus = json.load(f)
                    if int(focus.get("cycle", 0)) <= cycle:
                        focus["cycle"] = new_cycle
                        with open(focus_path, "w") as f:
                            json.dump(focus, f, indent=2)
                            f.write("\n")
                except Exception:
                    pass

            log.info("AUTO-INCREMENT: cycle %d already committed, bumped state to %d",
                     cycle, new_cycle)
            _emit("on_cycle_bumped", cycle, new_cycle)
    except Exception as e:
        log.warning("Auto-increment check failed: %s", e)


# ── Main agent loop ───────────────────────────────────────────────────

def _check_api_health(base_url, timeout=3):
    """Probe the LLM endpoint. Return (ok: bool, detail: str)."""
    try:
        resp = requests.get(f"{base_url}/health", timeout=timeout)
        if resp.status_code == 200:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except requests.Timeout:
        return False, "timeout"
    except requests.ConnectionError:
        return False, "unreachable"
    except requests.RequestException as e:
        return False, str(e)[:60]


def _list_available_models(base_url, timeout=3):
    """Query /v1/models and return a list of model id strings, or []."""
    try:
        resp = requests.get(f"{base_url}/v1/models", timeout=timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except (requests.RequestException, ValueError, KeyError):
        return []


def _render_context_bar(history, summary_state, ctx_size, width=30):
    """Return a multi-line string showing current context usage with a bar."""
    body_tokens = sum(_estimate_tokens(m) for m in history) if history else 0
    summary_text = summary_state.get("text", "") or ""
    summary_tokens = _estimate_tokens({"role": "system", "content": summary_text}) if summary_text else 0
    total = body_tokens + summary_tokens
    pct = total / ctx_size if ctx_size else 0.0
    bar = theme.bar(pct, width=width)
    pct_str = f"{pct*100:.1f}%"
    return (
        f"{bar} {pct_str}\n"
        f"  history: {body_tokens} tokens in {len(history)} messages\n"
        f"  summary: {summary_tokens} tokens\n"
        f"  budget:  {total} / {ctx_size} tokens"
    )


def _pick_model_interactive(current_model, base_url):
    """Interactive model picker. Returns the chosen model id or None."""
    models = _list_available_models(base_url)
    if not models:
        print(theme.c(theme.ROSE, f"Could not list models from {base_url}/v1/models"))
        return None
    print(theme.c(theme.SKY, f"Available models at {base_url}:"))
    for i, m in enumerate(models, 1):
        marker = theme.c(theme.MINT, " *") if m == current_model else "  "
        print(f"{marker} {i}. {m}")
    try:
        choice = input("Pick a model number (blank to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not choice:
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            return models[idx]
    except ValueError:
        pass
    print(theme.c(theme.ROSE, "Invalid selection."))
    return None


def run_agent_interactive(initial_prompt=None, auto=False, continue_mode=False, *, cb=None, tui=False):
    """Interactive agent that maintains conversation history.

    When `tui=True`, a prompt_toolkit front-end (tui.TuiSession) owns the
    input prompt and swaps the default TerminalCallbacks for TuiCallbacks
    so the bottom toolbar reflects live model/message/ctx state. The TUI
    is optional — if prompt_toolkit isn't installed, a clean ImportError
    is raised at TuiSession construction time.
    """

    ctx_size = _config["context"]["ctx_size"]
    max_tokens = _config["context"]["max_tokens"]
    gen = _config["generation"]

    log, log_path, error_log_path = _setup_logger()

    # Install the UI callback handle for this session
    global _cb, _cb_log
    _cb = cb if cb is not None else TerminalCallbacks()
    _cb_log = log

    model_name = _config["llm"]["model"]
    ok, detail = _check_api_health(BASE_URL)

    _emit("on_session_start", {
        "api_ok": ok,
        "api_detail": detail,
        "base_url": BASE_URL,
        "model": model_name,
        "ctx_size": ctx_size,
        "max_turns": _MAX_TURNS,
        "log_path": log_path,
        "error_log_path": error_log_path,
    })

    log.info("Session started | ctx_size=%d max_turns=%d temperature=%.1f max_tokens=%d",
             ctx_size, _MAX_TURNS, gen["temperature"], max_tokens)
    log.info("Tools registered: %s", [t["function"]["name"] for t in tools])

    # Create async summarizer if enabled and the CPU endpoint is reachable
    _async_summarizer = None
    summary_cfg = _config.get("summary", {})
    if summary_cfg.get("enabled"):
        summary_url = summary_cfg.get("base_url", "http://127.0.0.1:8082")
        try:
            health = requests.get(f"{summary_url}/health", timeout=3)
            if health.status_code == 200:
                _async_summarizer = AsyncSummarizer(_config, log)
                log.info("Async summarizer enabled → %s", summary_url)
                _emit("on_summarizer_status", "online", summary_url)
            else:
                log.warning("Summary endpoint returned %d, using main model for summaries",
                            health.status_code)
                _emit("on_summarizer_status", "unhealthy", str(health.status_code))
        except (requests.ConnectionError, requests.Timeout):
            log.warning("Summary endpoint unreachable at %s, using main model for summaries",
                        summary_url)
            _emit("on_summarizer_status", "offline", summary_url)

    # ── Continue mode: resume from checkpoint ──
    start_turn = 0
    if continue_mode:
        cp = _load_checkpoint()
        if cp:
            conversation_history, summary_state, start_turn, initial_files = cp
            log.info("CONTINUE: resuming from checkpoint (turn %d, %d messages)",
                     start_turn, len(conversation_history))
            # Cap summary from old checkpoints that may have bloated summaries
            if summary_state.get("text"):
                summary_state["text"] = _condense_summary(summary_state["text"], log)
            _emit("on_continue_resumed", start_turn, len(conversation_history))
            # Add a resume nudge so the model knows it's continuing
            conversation_history.append({"role": "user", "content":
                "Continue where you left off. The session was interrupted — "
                "pick up from your current phase and finish the cycle."})
            result = run_agent_single(conversation_history, summary_state, initial_files, log,
                                      gen["temperature"], gen["top_p"], gen["top_k"],
                                      gen["presence_penalty"], max_tokens, ctx_size,
                                      start_turn=start_turn,
                                      async_summarizer=_async_summarizer)
            if auto:
                cleanup_temp_sessions()
                _delete_checkpoint()
                log.info("Session ended (continue mode) | %d messages", len(conversation_history))
                return
            # Fall through to interactive loop if not auto
        else:
            _emit("on_continue_none")
            log.info("CONTINUE: no checkpoint found, starting fresh")

    if not continue_mode:
        # Check if the current cycle was already committed — bump if so
        _auto_increment_cycle(log)

    conversation_history = conversation_history if continue_mode and start_turn > 0 else []
    summary_state = summary_state if continue_mode and start_turn > 0 else {"text": "", "up_to": 0}
    initial_files = initial_files if continue_mode and start_turn > 0 else None

    # ── TUI front-end (optional) ──
    # Now that history / summary / initial_files have stable identities,
    # instantiate the prompt_toolkit session and swap the UI callback.
    tui_session = None
    if tui:
        import tui as _tuimod
        tui_session = _tuimod.TuiSession(
            history=conversation_history,
            summary_state=summary_state,
            config=_config,
            ctx_size=ctx_size,
            cb=_cb,
            estimate_tokens=_estimate_tokens,
        )
        _cb = _tuimod.TuiCallbacks(tui_session, verbose=getattr(_cb, "verbose", False))

    if initial_prompt and not (continue_mode and start_turn > 0):
        _emit("on_user_message", initial_prompt)
        expanded, files, err = _expand_file_refs(initial_prompt)
        if err:
            _emit("on_error", err)
            return
        if files:
            initial_files = files
        conversation_history.append({"role": "user", "content": expanded})
        log.info("USER: %s", expanded)
        result = run_agent_single(conversation_history, summary_state, initial_files, log,
                                  gen["temperature"], gen["top_p"], gen["top_k"],
                                  gen["presence_penalty"], max_tokens, ctx_size,
                                  async_summarizer=_async_summarizer)

        if auto:
            if result == "cancelled":
                # Double-escape in auto mode: prompt operator for guidance, then continue
                _emit("on_notice", "info",
                      f"\n{BOLD}[Agent paused — enter guidance, or press Enter to resume]{RESET}")
                try:
                    guidance = input("\nOperator: ").strip()
                except (EOFError, KeyboardInterrupt):
                    log.info("Session ended (operator cancelled) | %d messages", len(conversation_history))
                    print()
                    return
                if guidance:
                    expanded_g, files_g, err_g = _expand_file_refs(guidance)
                    if err_g:
                        _emit("on_error", err_g)
                    else:
                        if files_g:
                            initial_files = files_g
                        conversation_history.append({"role": "user", "content": expanded_g})
                        log.info("OPERATOR: %s", expanded_g)
                else:
                    conversation_history.append({"role": "user", "content":
                        "Continue where you left off. Finish your current cycle."})
                    log.info("OPERATOR: [resume — no guidance]")
                # Continue in auto mode until the agent finishes
                run_agent_single(conversation_history, summary_state, initial_files, log,
                                 gen["temperature"], gen["top_p"], gen["top_k"],
                                 gen["presence_penalty"], max_tokens, ctx_size,
                                 async_summarizer=_async_summarizer)
            cleanup_temp_sessions()
            _delete_checkpoint()
            log.info("Session ended (auto mode) | %d messages in history", len(conversation_history))
            return

    while True:
        try:
            if tui_session is not None:
                user_input = tui_session.prompt()
            else:
                user_input = input("\nYou: ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\n\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ["exit", "quit"]:
            print("Goodbye!")
            break
        if user_input.startswith("/"):
            def _refresh_cb_log(new_log):
                globals()["_cb_log"] = new_log
            ctx = SimpleNamespace(
                conversation_history=conversation_history,
                summary_state=summary_state,
                initial_files=initial_files,
                async_summarizer=_async_summarizer,
                cb=_cb,
                log=log,
                log_path=log_path,
                ctx_size=ctx_size,
                config=_config,
                base_url=BASE_URL,
                setup_logger=_setup_logger,
                pick_model=_pick_model_interactive,
                render_context_bar=_render_context_bar,
                refresh_cb_log=_refresh_cb_log,
            )
            if _commands.handle_command(user_input, ctx):
                # /clear may have rotated log and initial_files — pull them back
                initial_files = ctx.initial_files
                log = ctx.log
                log_path = ctx.log_path
                continue

        expanded, files, err = _expand_file_refs(user_input)
        if err:
            _emit("on_error", err)
            continue
        if files:
            initial_files = files

        conversation_history.append({"role": "user", "content": expanded})
        log.info("USER: %s", expanded)

        run_agent_single(conversation_history, summary_state, initial_files, log,
                         gen["temperature"], gen["top_p"], gen["top_k"],
                         gen["presence_penalty"], max_tokens, ctx_size,
                         async_summarizer=_async_summarizer)

    if tui_session is not None:
        tui_session.close()
    cleanup_temp_sessions()
    _delete_checkpoint()
    log.info("Session ended | %d messages in history", len(conversation_history))


def run_agent_single(conversation_history: list, summary_state: dict, initial_files,
                     log: logging.Logger, temperature=0.7, top_p=0.8, top_k=20,
                     presence_penalty=1.5, max_tokens=4096, ctx_size=32768,
                     start_turn=0, async_summarizer=None):
    """Run the agentic loop with turn limits and wind-down."""

    history_snapshot = len(conversation_history)
    turn = start_turn

    # Track repeated tool failures to break infinite loops
    _recent_tool_errors = []  # list of (tool_name, error_snippet)
    _REPEAT_THRESHOLD = 3    # inject forced think after this many identical failures

    # Track consecutive text-only responses (no tool calls).
    # Smaller models sometimes "think aloud" without calling a tool, intending
    # to continue on the next turn.  Auto-nudge up to _MAX_TEXT_ONLY times
    # before treating it as a real stop signal.
    _consecutive_text_only = 0

    # Detect degenerate text loops — model repeating the same output.
    # Store hashes of recent text-only responses; bail if too many match.
    _recent_text_hashes = []
    _TEXT_LOOP_THRESHOLD = 3
    _MAX_TEXT_ONLY = _config.get("cycle", {}).get("max_text_only", 3)

    # Disable auto-nudge after 'git push' — the cycle is done.
    _cycle_persisted = False

    _async_summarizer = async_summarizer

    while True:
        turn += 1

        # ── Wind-down and overtime warnings ──
        remaining = _MAX_TURNS - turn
        wind_down_msg = None
        if 0 < remaining <= _WIND_DOWN_TURNS:
            wind_down_msg = (
                f"[SYSTEM: {remaining} turns remaining before overtime. "
                f"Begin wrapping up — save your progress (CONSOLIDATE), "
                f"commit your work (PERSIST), and stop. "
                f"Do not start new tasks.]"
            )
            log.info("Wind-down: %d turns remaining", remaining)
        elif remaining <= 0:
            overtime = -remaining
            wind_down_msg = (
                f"[SYSTEM: You are {overtime} turns past the turn limit. "
                f"Finish what you are doing immediately — CONSOLIDATE and PERSIST now. "
                f"Do not start anything new.]"
            )
            log.warning("Overtime: %d turns past limit (%d)", overtime, _MAX_TURNS)

        # Harvest any completed async summary before building context
        if _async_summarizer and _async_summarizer.harvest(summary_state):
            log.info("Harvested async summary")
            _emit("on_summary_ready")

        # Build context window, with overflow reduction loop
        _ctx_max_messages = None  # None = use default _MAX_CONTEXT_MESSAGES
        _CTX_REDUCE_MAX = 10     # max number of message-reduction attempts

        for _ctx_attempt in range(_CTX_REDUCE_MAX + 1):
            messages_to_send, oldest_idx = _build_context(
                conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
                max_messages_override=_ctx_max_messages)

            # Summarize dropped messages: async (background) or sync (blocking)
            if _async_summarizer:
                unsummarized = oldest_idx - summary_state["up_to"]
                if unsummarized >= _SUMMARY_THRESHOLD and not _async_summarizer.is_running:
                    new_messages = conversation_history[summary_state["up_to"]:oldest_idx]
                    if new_messages:
                        _async_summarizer.kick(summary_state["text"], new_messages, oldest_idx)
                        log.info("Kicked async summary for %d messages", len(new_messages))
                        _emit("on_notice", "info", "[background summarization started]")
            elif _maybe_resummarize(conversation_history, summary_state, oldest_idx, log):
                messages_to_send, oldest_idx = _build_context(
                    conversation_history, summary_state, initial_files, ctx_size, max_tokens, log,
                    max_messages_override=_ctx_max_messages)

            # Inject wind-down as a system message at the end of context
            if wind_down_msg:
                messages_to_send.append({"role": "user", "content": wind_down_msg})

            log.info("--- Turn %d/%d | sending %d messages (history has %d total)",
                     turn, _MAX_TURNS, len(messages_to_send), len(conversation_history))

            # Call the model (streaming)
            request_body = {
                "model": _config["llm"]["model"],
                "messages": messages_to_send,
                "temperature": temperature,
                "top_p": top_p,
                "top_k": top_k,
                "presence_penalty": presence_penalty,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
                "cache_prompt": True,
                "tools": tools,
                "tool_choice": "auto",
                "stream": True,
            }

            try:
                response = _llm_request(log, json=request_body, stream=True, timeout=3600)
                log.info("Response status: %d", response.status_code)
                break  # success — exit the reduction loop
            except ContextOverflowError:
                if _ctx_attempt >= _CTX_REDUCE_MAX:
                    log.error("Context overflow: still failing after %d reductions", _CTX_REDUCE_MAX)
                    _emit("on_error", "Error: context overflow — could not fit in server context window")
                    return "error"
                current_count = len(messages_to_send)
                if current_count <= 2:
                    # Already at minimum messages — aggressively truncate the summary
                    # instead of trying to reduce messages further
                    if summary_state["text"]:
                        old_len = len(summary_state["text"])
                        summary_state["text"] = summary_state["text"][:old_len // 2]
                        log.warning("Context overflow (attempt %d/%d): at min messages, "
                                    "truncating summary from %d to %d chars",
                                    _ctx_attempt + 1, _CTX_REDUCE_MAX, old_len, len(summary_state["text"]))
                        _emit("on_notice", "info", "[Context overflow — truncating summary to fit]")
                    else:
                        log.error("Context overflow with no summary and minimal messages — cannot reduce further")
                        _emit("on_error", "Error: context overflow — cannot reduce further")
                        return "error"
                else:
                    # Reduce: cap messages to current count minus 2 (drop oldest pair)
                    _ctx_max_messages = max(2, current_count - 2)
                    log.warning("Context overflow (attempt %d/%d): reducing from %d to max %d messages",
                                _ctx_attempt + 1, _CTX_REDUCE_MAX, current_count, _ctx_max_messages)
                    _emit("on_context_recovery", True)
                    # Force a resummarize with the tighter window so dropped messages aren't lost
                    _maybe_resummarize(conversation_history, summary_state, oldest_idx, log, force=True)
                continue
            except requests.exceptions.RequestException as e:
                log.error("Request failed after retries: %s", e)
                _emit("on_error", f"Error calling server: {e}")
                return "error"

        # Accumulate streamed response
        content_parts = []
        tool_calls_by_index = {}
        printed_header = False
        receiving_tools = False
        status = StreamStatus()
        status.start("\nAssistant: ")
        renderer = _ReasoningRenderer(lambda t: _emit("on_stream_chunk", t))

        try:
            with cancellable():
                for line in response.iter_lines(decode_unicode=True):
                    check_cancelled()
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices")
                    if not choices:
                        continue  # skip usage/stats chunks
                    delta = choices[0].get("delta", {})

                    if delta.get("content"):
                        if not printed_header:
                            status.first_token()
                            printed_header = True
                        renderer.feed(delta["content"])
                        content_parts.append(delta["content"])
                        status.count_token()

                    if delta.get("tool_calls"):
                        if not receiving_tools:
                            receiving_tools = True
                            if printed_header:
                                print()
                                status = StreamStatus()
                                status.start(f"{DIM}  preparing tool calls ")
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta["index"]
                            if idx not in tool_calls_by_index:
                                tool_calls_by_index[idx] = {
                                    "id": tc_delta.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            tc = tool_calls_by_index[idx]
                            if tc_delta.get("id"):
                                tc["id"] = tc_delta["id"]
                            if tc_delta.get("function", {}).get("name"):
                                tc["function"]["name"] = tc_delta["function"]["name"]
                            if tc_delta.get("function", {}).get("arguments"):
                                tc["function"]["arguments"] += tc_delta["function"]["arguments"]
        except CancelledError:
            renderer.flush()
            status.finish()
            response.close()
            _emit("on_cancelled", "streaming")
            log.info("CANCELLED during streaming")
            # Keep partial history so caller can inject user guidance
            return "cancelled"

        renderer.flush()
        if content_parts and not receiving_tools:
            print()
        status.finish()

        full_content = _THINK_TAG_RE.sub('', "".join(content_parts)).strip()
        _emit("on_assistant_text", full_content, None)
        tool_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)] if tool_calls_by_index else []

        assistant_msg = {"role": "assistant", "content": full_content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        conversation_history.append(assistant_msg)

        if full_content:
            log.info("ASSISTANT: %s", full_content)

        # Detect degenerate text loops (model repeating itself)
        if full_content:
            import hashlib as _hl
            _text_hash = _hl.md5(full_content.encode()).hexdigest()
            _recent_text_hashes.append(_text_hash)
            # Keep only recent entries
            if len(_recent_text_hashes) > 10:
                _recent_text_hashes[:] = _recent_text_hashes[-10:]
            _repeat_count = sum(1 for h in _recent_text_hashes if h == _text_hash)
            if _repeat_count >= _TEXT_LOOP_THRESHOLD:
                log.warning("Text loop detected: same output %d times — stopping",
                            _repeat_count)
                _emit("on_text_loop_detected", _repeat_count)
                return "done"

        if not tool_calls:
            if not _NUDGE_ENABLED:
                log.info("Stopping: text-only response (no tool calls)")
                return "done"
            # If the cycle already persisted (git push happened), stop cleanly.
            if _cycle_persisted:
                log.info("Stopping: cycle already persisted (git push), no more nudges")
                return "done"
            # Past turn limit + no tool use = end cycle immediately
            if turn > _MAX_TURNS:
                log.warning("Overtime + text-only response — ending cycle")
                _emit("on_overtime", "text_only")
                return "done"
            _consecutive_text_only += 1
            if _consecutive_text_only >= _MAX_TEXT_ONLY:
                log.info("Stopping: %d consecutive text-only responses", _consecutive_text_only)
                return "done"

            # First text-only response: strip it from context and retry silently.
            # Leaving hallucinated content in history poisons subsequent turns —
            # the model builds on its fabricated answer instead of using tools.
            if _consecutive_text_only == 1:
                conversation_history.pop()  # remove the hallucinated assistant msg
                log.info("Hallucination guard: stripped text-only response, retrying")
                _emit("on_hallucination_stripped", "text_only")
                continue

            # Detect hallucinated file reads: model claims to have read a file
            # but _accessed_files doesn't show it.  Give a targeted correction.
            _hallucinated_read = False
            if full_content:
                try:
                    from tools.file import _accessed_files
                    import re as _re
                    _read_claims = _re.findall(
                        r'(?:read|found|contents? of|file (?:has|contains|shows))\s+[`"\']?(\S+\.(?:py|json|md|txt|yaml|yml|toml|jsonl|sh|cfg))',
                        full_content, _re.IGNORECASE
                    )
                    for claimed_file in _read_claims:
                        from pathlib import Path as _P
                        _resolved = str((_P.cwd() / claimed_file).resolve())
                        if _resolved not in _accessed_files:
                            _hallucinated_read = True
                            break
                except Exception:
                    pass

            if _hallucinated_read:
                # Strip the hallucinated message and give a pointed correction
                conversation_history.pop()
                nudge = (
                    "You did NOT actually read that file — you hallucinated its contents. "
                    "You MUST call the file tool with action='read' to see what a file contains. "
                    "Do not guess or fabricate file contents. Use the tool now."
                )
                log.info("Hallucination guard: detected fabricated file read, correcting")
                _emit("on_hallucination_stripped", "file_read")
            else:
                # Generic nudge
                nudge = (
                    "Continue — use your tools to take the next action. "
                    "Empirical > theoretical: if you suspect a bug, verify it "
                    "with a tool before trying to fix it. "
                    "Do not repeat your analysis, just act."
                )

            conversation_history.append({"role": "user", "content": nudge})
            log.info("Auto-nudge (%d/%d): text-only response, prompting to continue",
                     _consecutive_text_only, _MAX_TEXT_ONLY)
            continue

        _consecutive_text_only = 0  # reset on successful tool use

        # Execute tool calls
        log.info("Executing %d tool call(s)", len(tool_calls))
        _emit("on_tool_batch_start", len(tool_calls))
        try:
            with cancellable():
                for tool_call in tool_calls:
                    check_cancelled()
                    try:
                        if hasattr(tool_call, 'function'):
                            func_name = tool_call.function.name
                            raw_args = tool_call.function.arguments
                            tool_id = tool_call.id
                        else:
                            func_name = tool_call["function"]["name"]
                            raw_args = tool_call["function"]["arguments"]
                            tool_id = tool_call["id"]
                        func_args = json.loads(raw_args)
                        # Sanitize garbled Gemma 4 args that parsed as valid JSON
                        # e.g. {"action": "write**,content:"} — valid JSON but bogus values
                        func_args = _sanitize_tool_args(func_name, func_args, log)
                    except json.JSONDecodeError:
                        # Gemma 4 sometimes garbles arguments (e.g. "write**,content:")
                        # Try to salvage by extracting action from the mess
                        func_args = _salvage_tool_args(func_name, raw_args, log)
                        if func_args is None:
                            log.error("Unsalvageable tool args: %s | raw: %s", func_name, raw_args)
                            conversation_history.append({
                                "role": "tool", "tool_call_id": tool_id,
                                "name": func_name,
                                "content": f"Error: malformed arguments — could not parse. "
                                           f"Use separate JSON keys: {{\"action\": \"write\", \"path\": \"...\", \"content\": \"...\"}}"
                            })
                            continue
                    except Exception as e:
                        log.error("Error parsing tool call: %s | raw: %s", e, tool_call)
                        continue

                    # Validate required fields — catch cases where sanitizer
                    # extracted the action but lost required params (empty garble)
                    if func_name == "file" and isinstance(func_args, dict):
                        action = func_args.get("action", "")
                        if action in ("read", "write", "insert", "append", "delete") and "path" not in func_args:
                            log.warning("Sanitized file call missing 'path' — returning error")
                            conversation_history.append({
                                "role": "tool", "tool_call_id": tool_id,
                                "name": func_name,
                                "content": (
                                    f"Error: your tool call was garbled — 'path' is missing. "
                                    f"Use exec_command to write files instead. Example: "
                                    f'{{\"command\": \"cat > .agent/state/current-state.json << \'EOF\'\\n{{content}}\\nEOF\"}}'
                                ),
                            })
                            continue

                    log.info("TOOL CALL: %s(%s) [id=%s]", func_name, json.dumps(func_args), tool_id)

                    # Tools that do their own streaming (think) handle
                    # their own console output — don't wrap them in a spinner.
                    _STREAMING_TOOLS = {"think"}
                    use_spinner = func_name not in _STREAMING_TOOLS

                    if use_spinner:
                        tool_status = StreamStatus()
                        tool_status.start(f"  -> {func_name} ")

                    if func_name not in MAP_FN:
                        result_str = f"Error: Unknown tool '{func_name}'"
                    else:
                        try:
                            result_str = str(MAP_FN[func_name](**func_args))
                        except CircuitBreakerError as e:
                            # Tool temporarily unavailable - return graceful degradation
                            result_str = f"Tool '{func_name}' temporarily unavailable: {e}"
                        except Exception as e:
                            result_str = f"Error executing tool: {str(e)}"

                        # Conversational tool recovery: on error, try to fix params
                        if result_str.startswith("Error"):
                            try:
                                from tool_recovery import attempt_recovery
                                recovered = attempt_recovery(
                                    func_name, func_args, result_str,
                                    map_fn=MAP_FN,
                                    llm_call_fn=lambda **kw: _llm_request(log, **kw),
                                    config=_config, log=log,
                                )
                                if recovered is not None:
                                    result_str = recovered
                            except Exception as e:
                                log.debug("Tool recovery unavailable: %s", e)

                    if use_spinner:
                        tool_status.first_token()
                        tool_status.finish()
                    _emit("on_tool_start", func_name, func_args)

                    conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": func_name,
                        "content": result_str,
                    })

                    # Detect cycle completion: git push through exec_command
                    if func_name == "exec_command" and "git push" in func_args.get("command", ""):
                        _cycle_persisted = True
                        # Record cycle timestamp automatically
                        if _tracker:
                            try:
                                with open(_state_path("current-state.json")) as _sf:
                                    _cycle = json.load(_sf).get("cycle", 0)
                            except Exception:
                                _cycle = 0
                            try:
                                timestamp = _tracker.auto_record(agent_id="e1")
                                log.debug("Cycle %s recorded: %s", _cycle, timestamp)
                            except Exception as e:
                                log.error("Failed to record cycle timestamp: %s", e)
                        log.info("Cycle persist detected (git push) — auto-nudge disabled")

                    log.info("TOOL RESULT [%s]: %s", func_name, result_str)

                    # Track repeated tool results (errors and identical results)
                    _result_sig = (func_name, result_str[:100])
                    _recent_tool_errors.append(_result_sig)
                    _result_repeats = sum(1 for e in _recent_tool_errors if e == _result_sig)

                    # Past turn limit + same tool result 3 times = end cycle
                    if turn > _MAX_TURNS and _result_repeats >= 3:
                        log.warning("Overtime + repeated tool result (%s x%d) — ending cycle",
                                    func_name, _result_repeats)
                        _emit("on_overtime", "repeated_result")
                        return "done"

                    if result_str.startswith("Error"):
                        consecutive = _result_repeats
                        if consecutive >= _REPEAT_THRESHOLD * 2:
                            # Model is stuck even after forced thinks — skip this step
                            log.warning("Hard bail: %s failed %d times — skipping",
                                        func_name, consecutive)
                            _emit("on_tool_skip", func_name, consecutive)
                            conversation_history.append({
                                "role": "user",
                                "content": (
                                    f"SYSTEM: The {func_name} tool has failed {consecutive} times "
                                    f"with the same error. This step is being SKIPPED. "
                                    f"Use exec_command with cat/heredoc to write files instead, "
                                    f"or move on to the next step."
                                ),
                            })
                            _recent_tool_errors[:] = [e for e in _recent_tool_errors if e[0] != func_name]
                            break
                        elif consecutive >= _REPEAT_THRESHOLD:
                            # Force a think call to break the loop
                            think_prompt = (
                                f"MANDATORY REFLECTION: I have called {func_name} "
                                f"{consecutive} times and gotten the same error each time.\n\n"
                                f"The error is: {result_str[:300]}\n\n"
                                f"My last arguments were: {json.dumps(func_args) if isinstance(func_args, dict) else str(func_args)}\n\n"
                                f"I MUST answer these questions:\n"
                                f"1. What exactly is the error telling me?\n"
                                f"2. What parameter am I missing or getting wrong?\n"
                                f"3. What is a DIFFERENT way to accomplish my goal "
                                f"without repeating the same call?\n"
                                f"4. Should I just skip this step and move on to "
                                f"CONSOLIDATE and PERSIST?"
                            )
                            log.warning("Loop detected: %s x%d — forcing think",
                                        func_name, consecutive)
                            _emit("on_forced_think", func_name, consecutive)
                            if "think" in MAP_FN:
                                think_result = MAP_FN["think"](prompt=think_prompt)
                                # Inject as assistant thought + tool response
                                think_id = f"forced_think_{turn}_{consecutive}"
                                conversation_history.append({
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [{
                                        "id": think_id,
                                        "type": "function",
                                        "function": {
                                            "name": "think",
                                            "arguments": json.dumps({"prompt": think_prompt})
                                        }
                                    }]
                                })
                                conversation_history.append({
                                    "role": "tool",
                                    "tool_call_id": think_id,
                                    "name": "think",
                                    "content": str(think_result),
                                })
                                log.info("FORCED THINK RESULT: %s", think_result)
                    else:
                        # Successful call — reset tracker for this tool
                        _recent_tool_errors[:] = [e for e in _recent_tool_errors if e[0] != func_name]
                    _emit("on_tool_result", func_name, func_args, result_str,
                          result_str.startswith("Error"))
        except CancelledError:
            _emit("on_cancelled", "tool_execution")
            log.info("CANCELLED during tool execution")
            if _async_summarizer:
                _async_summarizer.drain()
                _async_summarizer.harvest(summary_state)
            _save_checkpoint(conversation_history, summary_state, turn, initial_files)
            return "cancelled"

        # Save checkpoint after each turn so -c can resume from here
        if _async_summarizer:
            _async_summarizer.harvest(summary_state)
        _save_checkpoint(conversation_history, summary_state, turn, initial_files)


def main():
    """Main entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="Agent with file tools")
    parser.add_argument("-a", "--auto", action="store_true",
                        help="Automation mode: run prompt and exit (no interactive loop)")
    parser.add_argument("-c", "--continue", dest="continue_mode", action="store_true",
                        help="Continue from last checkpoint (resume a crashed cycle)")
    parser.add_argument("-r", "--repeat", type=int, nargs="?", const=0, default=None,
                        help="Repeat N times (fresh each run). 0 or omit = indefinite. Implies -a.")
    parser.add_argument("--nudge", action="store_true",
                        help="Auto-nudge the model when it returns a text-only response.")
    parser.add_argument("--tui", action="store_true",
                        help="Use the prompt_toolkit TUI (bottom toolbar, completer, history). "
                             "Requires optional `prompt_toolkit` package. Interactive mode only.")
    parser.add_argument("prompt", nargs="*", help="Initial prompt")
    args = parser.parse_args()

    if args.tui and (args.auto or args.continue_mode or args.repeat is not None):
        parser.error("--tui is interactive only; cannot combine with -a/-c/-r")

    global _NUDGE_ENABLED
    _NUDGE_ENABLED = args.nudge

    initial_prompt = " ".join(args.prompt).strip() or None

    if args.continue_mode:
        run_agent_interactive(initial_prompt=initial_prompt, auto=True, continue_mode=True)
    elif args.repeat is not None:
        n = args.repeat
        run = 0
        try:
            while n == 0 or run < n:
                run += 1
                label = f"run {run}/{n}" if n > 0 else f"run {run}"
                _emit("on_repeat_run_start", label)
                run_agent_interactive(initial_prompt=initial_prompt, auto=True)
        except KeyboardInterrupt:
            _emit("on_repeat_done", run)
    else:
        run_agent_interactive(initial_prompt=initial_prompt, auto=args.auto, tui=args.tui)


if __name__ == "__main__":
    main()
