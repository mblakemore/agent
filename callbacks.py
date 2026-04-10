"""
Callback interface for the agent loop.

Separates the loop from the UI layer so the same loop can drive a plain
terminal, a richer prompt_toolkit TUI, or a silent automation host. The
`NullCallbacks` class is the canonical interface and does nothing; each
subclass overrides what it wants to render.

Rules (see plan/ui-upgrade-from-llmbox-cli.md § 7):
  * `log` is separate from callbacks — logs are full-fidelity, callbacks are
    presentation only (invariants D12).
  * No callback other than `check_cancelled()` may raise. The loop wraps
    every invocation in `_safe_cb` which catches and logs exceptions.
  * Callbacks are a keyword-only argument to `run_agent_single()` and are
    passed into `run_agent_interactive()`; the default is `TerminalCallbacks()`.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

import theme


# ── Null interface ──────────────────────────────────────────────────────

class NullCallbacks:
    """No-op base class. Every method is a stub that can be overridden.

    Subclasses should call `super().__init__()` if they add state so a
    future base-class initializer can run.
    """

    def __init__(self):
        pass

    # --- cancellation --------------------------------------------------
    def check_cancelled(self) -> None:
        return None

    # --- session lifecycle ---------------------------------------------
    def on_session_start(self, info: dict) -> None:
        return None

    def on_session_end(self, info: dict) -> None:
        return None

    def on_cycle_bumped(self, old: int, new: int) -> None:
        return None

    def on_continue_resumed(self, turn: int, messages: int) -> None:
        return None

    def on_continue_none(self) -> None:
        return None

    def on_repeat_run_start(self, label: str) -> None:
        return None

    def on_repeat_done(self, runs: int) -> None:
        return None

    # --- user input ----------------------------------------------------
    def on_user_message(self, text: str) -> None:
        return None

    def on_file_attached(self, header: str) -> None:
        return None

    # --- LLM API lifecycle ---------------------------------------------
    def on_api_start(self, label: str) -> None:
        return None

    def on_api_response(self) -> None:
        return None

    def on_api_done(self) -> None:
        return None

    def on_api_retry(self, error: str, attempt: int, max_attempts: int, delay: float) -> None:
        return None

    # --- assistant output ----------------------------------------------
    def on_stream_chunk(self, text: str) -> None:
        return None

    def on_assistant_text(self, text: str, reasoning: str | None) -> None:
        return None

    # --- tool loop -----------------------------------------------------
    def on_tool_batch_start(self, count: int) -> None:
        return None

    def on_tool_start(self, name: str, args: dict) -> None:
        return None

    def on_tool_result(self, name: str, args: dict, result: str, is_error: bool) -> None:
        return None

    def on_tool_skip(self, name: str, count: int) -> None:
        return None

    # --- per turn ------------------------------------------------------
    def on_turn_end(self, turn: int, turn_result: Any) -> None:
        return None

    # --- summarization -------------------------------------------------
    def on_summarizer_status(self, status: str, detail: str) -> None:
        return None

    def on_summary_start(self, count: int) -> None:
        return None

    def on_summary_done(self) -> None:
        return None

    def on_summary_ready(self) -> None:
        return None

    # --- recovery / guards (agent-specific, per D2) --------------------
    def on_forced_think(self, tool_name: str, count: int) -> None:
        return None

    def on_tool_recovery(self, name: str, attempt: int) -> None:
        return None

    def on_auto_nudge(self, n: int, max_n: int) -> None:
        return None

    def on_hallucination_stripped(self, kind: str) -> None:
        return None

    def on_text_loop_detected(self, count: int) -> None:
        return None

    def on_overtime(self, reason: str) -> None:
        return None

    def on_context_recovery(self, auto: bool) -> None:
        return None

    # --- errors / status -----------------------------------------------
    def on_notice(self, level: str, msg: str) -> None:
        """Low-stakes status messages that don't warrant a typed hook."""
        return None

    def on_error(self, msg: str) -> None:
        return None

    def on_cancelled(self, where: str) -> None:
        return None


# ── Terminal implementation ────────────────────────────────────────────

_COMPACT_LIMIT_DEFAULT = 400  # chars shown per tool result when compact


class TerminalCallbacks(NullCallbacks):
    """Plain-terminal UI. Aurora colors via theme.py, NO_COLOR-safe.

    State:
      verbose          — when True, tool results and reasoning print in full;
                         when False, long results are truncated to ~400 chars.
      tool_history     — deque of (name, args, result, is_error) tuples for /tools.
      _last_was_stream — set True after on_stream_chunk so on_assistant_text
                         doesn't double-print.
    """

    def __init__(self, *, verbose: bool = False, tool_history_size: int = 50,
                 compact_limit: int = _COMPACT_LIMIT_DEFAULT):
        super().__init__()
        self.verbose = verbose
        self.compact_limit = compact_limit
        self.tool_history: deque = deque(maxlen=tool_history_size)
        self._last_was_stream = False

    # -- helpers --------------------------------------------------------

    def _print(self, text: str = "", end: str = "\n") -> None:
        print(text, end=end, flush=True)

    def _note(self, text: str) -> None:
        self._print(theme.dim(f"  {text}"))

    def _compact_args(self, args: dict, max_val: int = 50) -> str:
        if not isinstance(args, dict):
            return str(args)[:max_val]
        parts = []
        for k, v in args.items():
            r = repr(v)
            if len(r) > max_val:
                r = r[:max_val] + "…"
            parts.append(f"{k}={r}")
        return ", ".join(parts)

    # -- session lifecycle ----------------------------------------------

    def on_session_start(self, info: dict) -> None:
        bar = theme.c(theme.VIOLET, "=" * 60, bold=True)
        title = theme.c(theme.VIOLET, "Agent with File Tools — Interactive Mode", bold=True)
        self._print(bar)
        self._print(title)
        self._print(bar)

        ok = info.get("api_ok", False)
        detail = info.get("api_detail", "")
        base_url = info.get("base_url", "")
        model = info.get("model", "")
        if ok:
            health = theme.c(theme.MINT, f"● {base_url} ({model})")
        else:
            health = theme.c(theme.AMBER, f"⚠ {base_url} ({detail}) — continuing anyway")
        self._print(f"API: {health}")
        self._print(f"Context size: {info.get('ctx_size')} tokens | Max turns: {info.get('max_turns')}")
        self._print(f"Session log: {info.get('log_path')}")
        self._print(f"Error log: {info.get('error_log_path')}")
        self._print(theme.dim("Press Escape twice to cancel. Type /help for commands."))
        self._print(theme.dim("Type 'exit' or 'quit' to end conversation.\n"))

    def on_summarizer_status(self, status: str, detail: str) -> None:
        msg = {
            "online":    f"[summary model online at {detail}]",
            "unhealthy": "[summary model unhealthy, using main model]",
            "offline":   "[summary model offline, using main model]",
        }.get(status, f"[summary status: {status}]")
        self._note(msg)

    def on_cycle_bumped(self, old: int, new: int) -> None:
        self._print(f"  [auto-increment: cycle {old} already committed → starting cycle {new}]")

    def on_continue_resumed(self, turn: int, messages: int) -> None:
        self._print(f"  [continuing from turn {turn} with {messages} messages]")

    def on_continue_none(self) -> None:
        self._print("  [no checkpoint found — starting fresh]")

    def on_repeat_run_start(self, label: str) -> None:
        self._print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")

    def on_repeat_done(self, runs: int) -> None:
        self._print(f"\n\nStopped after {runs} run(s).")

    # -- user input -----------------------------------------------------

    def on_user_message(self, text: str) -> None:
        self._print(f"You: {text}")

    def on_file_attached(self, header: str) -> None:
        self._note(header)

    # -- LLM retries ----------------------------------------------------

    def on_api_retry(self, error: str, attempt: int, max_attempts: int, delay: float) -> None:
        self._note(f"[LLM error: {error} — retry {attempt}/{max_attempts} in {delay}s]")

    # -- assistant output -----------------------------------------------

    def on_stream_chunk(self, text: str) -> None:
        self._last_was_stream = True
        print(text, end="", flush=True)

    def on_assistant_text(self, text: str, reasoning: str | None) -> None:
        # Streamed assistant text has already been printed chunk-by-chunk;
        # this hook is called at end-of-turn for completeness but must not
        # re-emit text when the stream was live.
        if self._last_was_stream:
            self._last_was_stream = False
            return
        if text:
            self._print(text)

    # -- tool loop ------------------------------------------------------

    def on_tool_batch_start(self, count: int) -> None:
        self._print(theme.dim(f"\nExecuting {count} tool call(s)..."))

    def on_tool_start(self, name: str, args: dict) -> None:
        self._print(f"{theme.CLEAR_LINE}  -> {name}({self._compact_args(args)})")

    def on_tool_result(self, name: str, args: dict, result: str, is_error: bool) -> None:
        # D12 invariant: this callback only styles output. The raw `result`
        # string has already been logged and appended to the conversation
        # history by the loop; compacting here never touches either.
        self.tool_history.append((name, dict(args) if isinstance(args, dict) else args,
                                  result, is_error))

        if self.verbose or len(result) <= self.compact_limit:
            display = result
        else:
            head = result[: self.compact_limit]
            display = head + theme.dim(
                f"\n    … [truncated {len(result) - self.compact_limit} chars — /verbose for full]"
            )

        color = theme.ROSE if is_error else None
        if color:
            self._print(f"    Result: {theme.c(color, display)}")
        else:
            self._print(f"    Result: {display}")

    def on_tool_skip(self, name: str, count: int) -> None:
        self._print(f"  [skipping — {name} failed {count} times]")

    # -- guards ---------------------------------------------------------

    def on_forced_think(self, tool_name: str, count: int) -> None:
        self._print(f"  [loop detected — forcing think]")

    def on_tool_recovery(self, name: str, attempt: int) -> None:
        self._note(f"[tool recovery: {name} attempt {attempt}]")

    def on_auto_nudge(self, n: int, max_n: int) -> None:
        self._note(f"[auto-nudge {n}/{max_n}]")

    def on_hallucination_stripped(self, kind: str) -> None:
        if kind == "file_read":
            self._print("  [hallucinated file read detected, correcting]")
        elif kind == "text_only":
            self._print("  [text-only response stripped, retrying]")
        else:
            self._print(f"  [hallucination stripped: {kind}]")

    def on_text_loop_detected(self, count: int) -> None:
        self._print(f"  [text loop detected — stopping]")

    def on_overtime(self, reason: str) -> None:
        mapping = {
            "text_only":       "[overtime + no tool use — ending cycle]",
            "repeated_result": "[overtime + repeated result — ending cycle]",
        }
        self._print(f"  {mapping.get(reason, f'[overtime: {reason}]')}")

    def on_context_recovery(self, auto: bool) -> None:
        self._note("[context overflow — trimming and retrying]")

    # -- summarization --------------------------------------------------

    def on_summary_start(self, count: int) -> None:
        if count > 0:
            self._note(f"[summarizing {count} messages...]")
        else:
            self._note("[summary too long, condensing...]")

    def on_summary_done(self) -> None:
        self._note("[summary updated]")

    def on_summary_ready(self) -> None:
        self._note("[summary ready]")

    # -- errors / status ------------------------------------------------

    def on_notice(self, level: str, msg: str) -> None:
        if level == "warn":
            self._print(theme.c(theme.AMBER, f"  {msg}"))
        elif level == "error":
            self._print(theme.c(theme.ROSE, f"  {msg}"))
        else:
            self._note(msg)

    def on_error(self, msg: str) -> None:
        self._print(theme.c(theme.ROSE, msg))

    def on_cancelled(self, where: str) -> None:
        self._print(theme.c(theme.AMBER, f"\n[cancelled]"))

    # -- /tools viewer --------------------------------------------------

    def render_tools(self, limit: int = 20) -> str:
        if not self.tool_history:
            return "No tool calls yet."
        lines = [theme.c(theme.SKY, f"Last {min(limit, len(self.tool_history))} tool call(s):")]
        tail = list(self.tool_history)[-limit:]
        for i, (name, args, result, is_error) in enumerate(tail, 1):
            marker = theme.c(theme.ROSE, "✗") if is_error else theme.c(theme.MINT, "✓")
            head = result.split("\n", 1)[0][:120]
            lines.append(f"  {marker} {i}. {name}({self._compact_args(args, 40)})")
            lines.append(theme.dim(f"      → {head}"))
        return "\n".join(lines)


# ── helper: safe invocation ────────────────────────────────────────────

def safe_cb(cb: NullCallbacks, method: str, *args, log=None, **kwargs) -> Any:
    """Invoke a callback method, swallowing any exception.

    The loop must never crash because of a buggy UI hook. If a method is
    missing (e.g., third-party subclass diverged from this interface),
    that is also treated as a no-op.
    """
    fn = getattr(cb, method, None)
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if log is not None:
            try:
                log.exception("Callback %s raised: %s", method, e)
            except Exception:
                pass
        return None
