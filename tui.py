"""
prompt_toolkit-based TUI front-end for the interactive agent.

This is the default interactive front-end — `main()` instantiates a
`TuiSession` whenever it enters an interactive loop unless the caller
passes `--no-tui`. `prompt_toolkit` itself is an optional runtime
dependency: if the import fails at module load time, `_AVAILABLE` is
set to `False` and `main()` falls back to a plain `input()` prompt
with a one-line notice. A caller that bypasses that fallback and
instantiates `TuiSession()` directly while `prompt_toolkit` is missing
gets a clean `ImportError` from the stub class below.

Design notes (see plan/ui-upgrade-from-llmbox-cli.md § Phase 3):
  * TuiSession owns the PromptSession and bottom toolbar. It holds
    references to mutable loop state (history, summary_state, config)
    so the toolbar renders live values on every keystroke.
  * LlmboxCompleter completes slash commands and @path references.
  * Enter submits, Ctrl+N inserts a literal newline.
  * TuiCallbacks inherits from TerminalCallbacks so D12 invariants
    (raw result_str in tool_history, compaction for display only) are
    unchanged between plain and TUI runs.
  * cancel.set_tui_mode is flipped True only while TuiSession.prompt()
    is active — the agent's cbreak monitor stays live during streaming
    so double-escape still cancels ongoing turns.
"""

from __future__ import annotations

import html
import os
import threading
import time
from pathlib import Path
from typing import Callable

import cancel
import spinner as _spinner
import theme as _theme
from callbacks import TerminalCallbacks


# Thread-local flag: True on a thread while its TuiSession.prompt() is
# actively blocked reading user input. Read from TuiCallbacks._print so
# that a background-thread print (e.g. on_summary_ready from the async
# summarizer) can wrap itself in patch_stdout only when it would otherwise
# corrupt the rendered prompt. If a hook fires on a thread that never
# entered prompt(), the flag is False and no wrapping is needed — that
# thread doesn't own the terminal anyway.
_prompt_active = threading.local()

# Sentinel returned by TuiSession.prompt() when an idle prompt was woken by a
# background monitor (rather than by the user typing). The caller detects it by
# identity and drains the monitor bus instead of treating it as typed input.
MONITOR_WAKE = object()


def _prompt_is_active() -> bool:
    return getattr(_prompt_active, "on", False)


def _shorten_cwd(path: str) -> str:
    """Shorten `path` for display in the toolbar.

    1. If a top-level symlink (e.g. `/droid` → `/mnt/droid`) covers the
       path, prefer the symlinked form — `/droid/repos/x` reads cleaner
       than `/mnt/droid/repos/x`.
    2. Collapse `$HOME` to `~`.
    """
    try:
        candidates = []
        for name in os.listdir("/"):
            entry = "/" + name
            try:
                if not os.path.islink(entry):
                    continue
                target = os.path.realpath(entry)
            except OSError:
                continue
            if path == target or path.startswith(target + os.sep):
                candidates.append((entry, target))
        if candidates:
            entry, target = max(candidates, key=lambda et: len(et[1]))
            path = entry + path[len(target):]
    except OSError:
        pass
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home):]
    return path


try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion, PathCompleter
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output.color_depth import ColorDepth
    from prompt_toolkit.styles import Style
    _AVAILABLE = True
    _IMPORT_ERROR: Exception | None = None
except ImportError as _e:
    _AVAILABLE = False
    _IMPORT_ERROR = _e


# Aurora palette restated as hex — prompt_toolkit has its own style system,
# so we can't reuse theme.py's ANSI escapes directly.
_VIOLET_HEX = "#7b4dff"
_SKY_HEX    = "#35c2f5"
_MINT_HEX   = "#5fffb0"
_AMBER_HEX  = "#ffbe3d"
_ROSE_HEX   = "#ff4d6d"

# Bottom-toolbar palette — dark neutral background with high-contrast text.
# Using a flat dark-gray / off-white pair gives uniform readability regardless
# of terminal theme, and avoids the low-contrast sky+violet clash.
_BAR_BG_HEX = "#0d0d0d"
_BAR_BG2_HEX = "#000000"
_BAR_FG_HEX = "#a8a8a8"

_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help",    "show available commands"),
    ("/clear",   "clear conversation history"),
    ("/context", "show current context usage"),
    ("/model",   "pick a different model"),
    ("/verbose", "toggle compact/full tool output"),
    ("/tools",   "show buffered tool calls — /tools N or /tools all"),
]


def _last_at_ref(text: str) -> int | None:
    """Return index of the last `@` that could start a file ref, else None.

    A valid `@ref` is preceded by whitespace or BOL and has no whitespace
    after it up to the cursor — same rule as agent._FILE_REF.
    """
    i = text.rfind("@")
    if i < 0:
        return None
    if i > 0 and not text[i - 1].isspace():
        return None
    if any(ch.isspace() for ch in text[i + 1:]):
        return None
    return i


# ── prompt_toolkit-dependent symbols ──────────────────────────────────
# Only defined when the package imports cleanly; the stubs at the bottom
# take over otherwise so `from tui import TuiSession` always works.

if _AVAILABLE:

    def _build_style() -> Style:
        return Style.from_dict({
            "prompt":                             f"{_VIOLET_HEX} bold",
            "prompt-spinner":                     f"{_MINT_HEX}",
            "prompt-cancelling":                  f"{_AMBER_HEX}",
            "bottom-toolbar":                     f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX} noreverse",
            "bottom-toolbar.cwd":                 f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX} bold",
            "bottom-toolbar.sep":                 f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
            "bottom-toolbar.model":               f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
            "bottom-toolbar.msgs":                f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
            "bottom-toolbar.ctx":                 f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
            "bottom-toolbar.verbose-on":          f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
            "bottom-toolbar.verbose-off":         f"bg:{_BAR_BG_HEX} {_BAR_FG_HEX}",
            "completion-menu.completion":         f"bg:{_VIOLET_HEX} #ffffff",
            "completion-menu.completion.current": f"bg:{_MINT_HEX} #000000 bold",
        })


    class LlmboxCompleter(Completer):
        """Slash-command + @path completer."""

        def __init__(self) -> None:
            self._path = PathCompleter(expanduser=True)

        def get_completions(self, document: Document, complete_event):
            text = document.text_before_cursor

            # Slash command at start of line — match prefix case-insensitively
            # so `/HELP`, `/HeLp`, `/h` all complete to the canonical `/help`.
            if text.startswith("/") and " " not in text:
                lower = text.lower()
                for cmd, desc in _SLASH_COMMANDS:
                    if cmd.startswith(lower):
                        yield Completion(
                            cmd,
                            start_position=-len(text),
                            display=cmd,
                            display_meta=desc,
                        )
                return

            # @path ref — defer to PathCompleter on a sub-document.
            at = _last_at_ref(text)
            if at is not None:
                prefix = text[at + 1:]
                sub = Document(text=prefix, cursor_position=len(prefix))
                for comp in self._path.get_completions(sub, complete_event):
                    yield comp


    # Double-press window for the cancel key. Generous enough to absorb the
    # parser's escape-flush delay (ttimeoutlen, tuned to 0.2s below): the
    # second physical ESC surfaces as a key up to one flush interval after
    # the first, so the observed gap between handler calls exceeds the gap
    # between keypresses.
    _CANCEL_WINDOW_S = 0.6

    def _build_key_bindings(
        enable_cancel: bool = False,
        on_cancel: Callable[[], None] | None = None,
    ) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _(event) -> None:
            event.current_buffer.validate_and_handle()

        @kb.add("c-n")
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        if enable_cancel:
            # Concurrent-input mode owns the keyboard continuously, so the
            # cbreak double-escape monitor is disabled (cancel.set_tui_mode);
            # cancellation must come from a binding here. The old
            # ("escape","escape") chord sat behind prompt_toolkit's
            # sequence-ambiguity timeout — the first ESC was held while PT
            # waited to see if a longer match followed, which made esc-esc
            # feel dead. Instead: an EAGER single-escape handler with its
            # own double-press window (mirrors cancel.py's cbreak monitor).
            # Fires the instant the second Escape key arrives. Trade-off,
            # documented: eager escape shadows meta-key (Alt-x) emacs
            # bindings inside the prompt; none are advertised in this UI.
            state = {"t": 0.0}

            @kb.add("escape", eager=True)
            def _(event) -> None:
                buf = event.current_buffer
                if getattr(buf, "complete_state", None) is not None:
                    # ESC while the completion menu is open keeps its
                    # conventional meaning: dismiss the menu, don't count
                    # toward a cancel.
                    buf.cancel_completion()
                    state["t"] = 0.0
                    return
                now = time.monotonic()
                if now - state["t"] <= _CANCEL_WINDOW_S:
                    state["t"] = 0.0
                    if on_cancel is not None:
                        on_cancel()
                    else:
                        cancel.request_cancel()
                else:
                    state["t"] = now

        return kb


    class TuiSession:
        """PromptSession wrapper with live bottom toolbar.

        Holds references to mutable loop state so the toolbar callback
        reflects the current model, message count, ctx% and verbose
        state on every keystroke without needing explicit refreshes.
        """

        # Sentinel prompt() returns when woken by a monitor (see wake()).
        WAKE = MONITOR_WAKE

        def __init__(
            self,
            *,
            history: list,
            summary_state: dict,
            config: dict,
            ctx_size: int,
            cb: TerminalCallbacks,
            estimate_tokens: Callable[[dict], int],
            enable_cancel_key: bool = False,
        ) -> None:
            self.history = history
            self.summary_state = summary_state
            self.config = config
            self.ctx_size = ctx_size
            self.cb = cb
            self._estimate_tokens = estimate_tokens
            # Toolbar ctx% cache. Key is (len(history), len(summary_text)) —
            # cheap to compute, catches both "new message appended" and
            # "summary updated in place". The rare case where the summary
            # is rewritten to the same length shows a stale percentage for
            # exactly one keystroke — acceptable staleness.
            self._ctx_cache_key: tuple[int, int] | None = None
            self._ctx_cache_val: float = 0.0
            # True between an esc-esc press and the turn acknowledging the
            # cancel — the toolbar renders "⏳ cancelling…" so the operator
            # sees the intent registered even while a blocking tool takes a
            # second to notice the flag.
            self.cancelling = False

            self._session: PromptSession = PromptSession(
                message=self._prompt_message,
                multiline=False,
                key_bindings=_build_key_bindings(
                    enable_cancel=enable_cancel_key,
                    on_cancel=self._on_cancel_key,
                ),
                completer=LlmboxCompleter(),
                complete_while_typing=False,
                bottom_toolbar=self._toolbar,
                style=_build_style(),
                color_depth=ColorDepth.TRUE_COLOR,
                enable_history_search=True,
                history=InMemoryHistory(),
            )
            if enable_cancel_key:
                # Shorten the parser's lone-ESC flush so the double-press
                # lands fast (default 0.5s made even the eager binding lag).
                try:
                    self._session.app.ttimeoutlen = 0.2
                except Exception:
                    pass

        @staticmethod
        def _plain(text: str, limit: int) -> str:
            """Escape- and control-free text, middle-truncated to limit.

            Everything rendered into the prompt/toolbar goes through here:
            prompt fragments tolerate no raw control bytes (and the toolbar's
            HTML() path parses XML), so sanitize once, uniformly.
            """
            text = _theme.truncate_middle(text, limit)
            text = _theme.strip_ansi(text)
            return "".join(ch for ch in text if ch >= " " or ch == "\t")

        def _prompt_message(self):
            """Prompt text, re-evaluated on every render.

            While the agent works, the spinner lives HERE — on the input
            line — not only in the bottom toolbar: prompt_toolkit hides the
            toolbar entirely when it cannot learn the terminal height (CPR
            unsupported — observed in the field), which made the toolbar
            spinner invisible exactly when it mattered. The prompt line is
            the one surface the app always renders. Animated by spinner's
            invalidate ticker. Must never raise (render path).
            """
            try:
                if self.cancelling:
                    return [
                        ("class:prompt", "\n"),
                        ("class:prompt-cancelling", "⏳ cancelling… "),
                        ("class:prompt", "You: "),
                    ]
                act = _spinner.get_active()
                if act is None:
                    return [("class:prompt", "\nYou: ")]
                label, t0 = act
                elapsed = time.monotonic() - t0
                seg = f"{_spinner.frame_at(elapsed)} {label} {elapsed:.1f}s"
                tail = getattr(self.cb, "stream_tail", "")
                if tail:
                    seg += f" · {tail}"
                try:
                    width = os.get_terminal_size().columns
                except OSError:
                    width = 80
                seg = self._plain(seg, max(24, width - 12))
                return [
                    ("class:prompt", "\n"),
                    ("class:prompt-spinner", seg + " "),
                    ("class:prompt", "You: "),
                ]
            except Exception:
                return [("class:prompt", "\nYou: ")]

        def _on_cancel_key(self) -> None:
            """esc-esc handler: request cancellation + visible feedback."""
            self.cancelling = True
            cancel.request_cancel()

        def clear_cancelling(self) -> None:
            """Drop the '⏳ cancelling…' toolbar segment (turn boundary)."""
            self.cancelling = False

        # -- public API ------------------------------------------------

        def prompt(self) -> str:
            """Read one line from the user. Returns stripped text.

            TUI mode is flipped on only while the prompt is active so the
            cbreak-based double-escape monitor can still intercept cancels
            during streaming (between prompts). The _prompt_active flag
            is set in lockstep so TuiCallbacks._print can route through
            patch_stdout while a prompt is displayed.
            """
            _prompt_active.on = True
            cancel.set_tui_mode(True)
            try:
                res = self._session.prompt()
            finally:
                cancel.set_tui_mode(False)
                _prompt_active.on = False
            # A background monitor woke the idle prompt: hand the sentinel back
            # (by identity) so the caller drains the monitor bus rather than
            # treating it as typed text.
            if res is MONITOR_WAKE:
                return MONITOR_WAKE
            return res.strip()

        def prompt_line(self) -> str:
            """Blocking read of one user line, for the concurrent-input thread.

            Unlike prompt(): no monitor-wake sentinel (the main loop consumes
            monitor pings from the queue directly, so the input prompt only ever
            returns typed text) and no tui_mode / _prompt_active toggling (the
            concurrent loop sets tui_mode once for the whole session and wraps
            the process in patch_stdout, so output routing is handled globally).
            """
            return self._session.prompt().strip()

        def wake(self) -> None:
            """Wake an idle prompt so it returns MONITOR_WAKE (thread-safe).

            Called from a monitor thread when new output is queued. No-op
            unless a prompt is actively running AND its input buffer is empty —
            so it never interrupts a running turn and never clobbers a
            half-typed line. The actual app.exit runs on the app's own event
            loop via call_soon_threadsafe. Never raises.
            """
            try:
                app = self._session.app
            except Exception:
                return
            if app is None or not getattr(app, "is_running", False):
                return
            loop = getattr(app, "loop", None)
            if loop is None:
                return

            def _do():
                try:
                    if app.is_running and not app.current_buffer.text:
                        app.exit(result=MONITOR_WAKE)
                except Exception:
                    pass

            try:
                loop.call_soon_threadsafe(_do)
            except Exception:
                pass

        def exit_app(self) -> None:
            """Stop a running prompt app from another thread (idempotent).

            Used by the live-input host at shutdown: schedules
            app.exit(exception=EOFError) on the app's own event loop, so a
            prompt_line() blocked in the input thread raises EOFError, the
            thread's patch_stdout context unwinds, and prompt_toolkit
            erases its render — leaving the terminal on a clean line
            instead of mid-paint. No-op when no app is running. Never
            raises.
            """
            try:
                app = self._session.app
            except Exception:
                return
            if app is None or not getattr(app, "is_running", False):
                return
            loop = getattr(app, "loop", None)
            if loop is None:
                return

            def _do():
                try:
                    if app.is_running:
                        app.exit(exception=EOFError)
                except Exception:
                    pass

            try:
                loop.call_soon_threadsafe(_do)
            except Exception:
                pass

        def set_cb(self, cb: TerminalCallbacks) -> None:
            """Swap the callback reference (toolbar reads verbose from it)."""
            self.cb = cb

        def close(self) -> None:
            """Release TUI mode unconditionally (idempotent)."""
            cancel.set_tui_mode(False)

        # -- toolbar ---------------------------------------------------

        def _ctx_pct(self) -> float:
            if not self.ctx_size:
                return 0.0
            summary = self.summary_state.get("text") or ""
            key = (len(self.history), len(summary))
            if self._ctx_cache_key == key:
                return self._ctx_cache_val
            body = sum(self._estimate_tokens(m) for m in self.history) if self.history else 0
            if summary:
                body += self._estimate_tokens({"role": "system", "content": summary})
            pct = min(1.0, body / self.ctx_size)
            self._ctx_cache_key = key
            self._ctx_cache_val = pct
            return pct

        def _spinner_segment(self, width: int) -> tuple[str, str]:
            """(plain, html) toolbar segment for an active spinner, or ("", "").

            Renders `⠙ label 12.3s` plus the live tail of the in-progress
            streamed line when one exists — this is the progress feedback
            live-input mode lost when the \\r spinner was suppressed under
            patch_stdout. Animated by spinner's invalidate ticker.
            """
            act = _spinner.get_active()
            if act is None:
                return "", ""
            label, t0 = act
            elapsed = time.monotonic() - t0
            seg = f" {_spinner.frame_at(elapsed)} {label} {elapsed:.1f}s"
            tail = getattr(self.cb, "stream_tail", "")
            if tail:
                seg += f" · {tail}"
            # Cap at half the bar so cwd/model/ctx stay visible. _plain
            # strips escapes and control bytes — truncate_middle inserts a
            # RESET escape before its marker, and HTML() parses via expat
            # where ANY raw control byte is an invalid XML token that
            # crashes the whole render loop (field crash 2026-08-13).
            seg = self._plain(seg, max(16, width // 2)) + " │"
            seg_html = (
                f'<style fg="{_MINT_HEX}" bg="{_BAR_BG_HEX}">{html.escape(seg)}</style>'
            )
            return seg, seg_html

        def _toolbar(self):
            # Escape anything user/config-controlled that lands in HTML() —
            # expat treats a stray & or < as fatally as the ESC byte above.
            cwd = html.escape(Path(os.getcwd()).name or "/")
            model = html.escape(self.config.get("llm", {}).get("model", "?"))
            msgs = len(self.history)
            pct = self._ctx_pct() * 100.0
            verbose = bool(getattr(self.cb, "verbose", False))

            # Drop the ~ approximation marker when the real tokenizer is in
            # use. Import lazily so tui.py stays independent of token_utils.
            try:
                from token_utils import _EXACT_TOKENIZER_AVAILABLE as _exact
            except Exception:
                _exact = False
            ctx_label = f"ctx {pct:.0f}%" if _exact else f"ctx ~{pct:.0f}%"

            verbose_segment = (
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> │ </style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> verbose </style>'
            ) if verbose else ""

            left = (
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"><b> {cwd} </b></style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> │ </style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> {model} </style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> │ </style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> {msgs} msgs </style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> │ </style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}"> {ctx_label} </style>'
                + verbose_segment
            )

            # Right-aligned hint: minimal in-bar reminder of the two
            # keys an operator most often needs.
            right_hint = "esc·esc cancel  │  /help "
            right = (
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG_HEX}">{right_hint}</style>'
            )

            # Pad to terminal width so the bar spans the screen.
            try:
                width = os.get_terminal_size().columns
            except OSError:
                width = 80
            spin_plain, spin_html = self._spinner_segment(width)
            if self.cancelling:
                # Cancel feedback outranks the spinner segment.
                spin_plain = " ⏳ cancelling… │"
                spin_html = (
                    f'<style fg="{_AMBER_HEX}" bg="{_BAR_BG_HEX}">'
                    f"{html.escape(spin_plain)}</style>"
                )
            left = spin_html + left
            visible_base = f" {cwd}  │  {model}  │  {msgs} msgs  │  {ctx_label} "
            visible_len = (
                len(spin_plain)
                + len(visible_base)
                + (len("  │   verbose ") if verbose else 0)
                + len(right_hint)
            )
            pad = max(1, width - visible_len)

            # Second line: full working directory on the left, TUI key
            # hints right-aligned. Leading `: ` distinguishes the
            # toolbar from a bash prompt.
            full_cwd = _shorten_cwd(os.getcwd())
            cwd_text = f" : {full_cwd} "
            right2 = "enter submit  │  ctrl-N newline  │  ↑↓ history "
            cwd_pad = max(1, width - len(cwd_text) - len(right2))
            second = (
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG2_HEX}">{html.escape(cwd_text)}</style>'
                f'<style bg="{_BAR_BG2_HEX}">{" " * cwd_pad}</style>'
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG2_HEX}">{right2}</style>'
            )

            return HTML(
                left
                + f'<style bg="{_BAR_BG_HEX}">{" " * pad}</style>'
                + right
                + "\n"
                + second
            )


    class TuiCallbacks(TerminalCallbacks):
        """TerminalCallbacks with a TUI-aware session_start banner.

        Inherits compaction, tool_history recording, streaming de-dup,
        and /tools rendering unchanged — D12 invariants hold because
        the raw result_str path is untouched. _print is routed through
        prompt_toolkit.patch_stdout while a TuiSession prompt is active
        so background-thread prints (e.g. on_summary_ready from the
        async summarizer) don't corrupt the rendered prompt line.
        """

        def __init__(self, tui_session: "TuiSession", *, verbose: bool = False) -> None:
            super().__init__(verbose=verbose)
            self.tui = tui_session
            self.tui.set_cb(self)
            # Pending partial line of streamed assistant text (live-input
            # mode only). Under patch_stdout a partial line shares its row
            # with the redrawn prompt and is erased by the next flush — so
            # on_stream_chunk buffers here and emits complete lines only.
            self._stream_buf = ""

        # -- live-input streaming (defect #2) --------------------------

        @staticmethod
        def _live_app_active() -> bool:
            """True while a prompt_toolkit app owns the terminal.

            In concurrent live-input mode the input thread runs the prompt
            continuously and holds a process-wide patch_stdout; the app is
            visible from any thread via the shared default AppSession.
            """
            try:
                from prompt_toolkit.application import get_app_or_none
                return get_app_or_none() is not None
            except Exception:
                return False

        def on_stream_chunk(self, text: str) -> None:
            if not self._live_app_active():
                super().on_stream_chunk(text)
                return
            # patch_stdout renders by erase-prompt → write → redraw-prompt:
            # only text ending in "\n" survives in scrollback. Buffer the
            # stream and emit whole lines; the final partial line is
            # committed by _flush_stream_buf at the next non-stream output
            # or at end of turn (on_assistant_text).
            self._last_was_stream = True
            self._stream_buf += text
            if "\n" in self._stream_buf:
                complete, self._stream_buf = self._stream_buf.rsplit("\n", 1)
                print(complete + "\n", end="", flush=True)

        def _flush_stream_buf(self) -> None:
            """Commit any pending partial stream line to scrollback."""
            if self._stream_buf:
                buf, self._stream_buf = self._stream_buf, ""
                print(buf, flush=True)

        @property
        def stream_tail(self) -> str:
            """Last ~50 visible chars of the in-progress streamed line.

            Rendered by the toolbar spinner segment so the operator sees
            generation progressing between the newlines that commit whole
            lines to scrollback.
            """
            tail = self._stream_buf.rsplit("\n", 1)[-1]
            return _theme.strip_ansi(tail)[-50:]

        def on_assistant_text(self, text: str) -> None:
            # End of turn: the base class skips re-printing streamed text
            # without touching _print, so flush the tail explicitly here.
            self._flush_stream_buf()
            super().on_assistant_text(text)

        def _print(self, text: str = "", end: str = "\n") -> None:
            # All non-stream output funnels through here — flushing the
            # stream buffer first guarantees scrollback ordering (partial
            # response line lands before the notice/tool header that
            # interrupted it).
            self._flush_stream_buf()
            if _prompt_is_active():
                from prompt_toolkit.patch_stdout import patch_stdout
                with patch_stdout(raw=True):
                    print(text, end=end, flush=True)
            else:
                print(text, end=end, flush=True)

        def on_cancelled(self, where: str) -> None:
            # The turn has acknowledged the cancel — drop the toolbar's
            # "⏳ cancelling…" segment before the banner prints.
            self.tui.clear_cancelling()
            super().on_cancelled(where)

        def on_session_start(self, info: dict) -> None:
            # Base banner already renders main+summary. TUI key hints now
            # live in the bottom-toolbar right-align so they don't take
            # up a line in the scrollback.
            super().on_session_start(info)


else:  # prompt_toolkit not installed — provide stubs that fail clearly

    _INSTALL_HINT = (
        "Interactive TUI mode requires the optional `prompt_toolkit` package. "
        "Install it with `pip install prompt_toolkit`, or pass `--no-tui` "
        "to use the plain `input()` prompt instead."
    )


    class TuiSession:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise ImportError(_INSTALL_HINT) from _IMPORT_ERROR


    class TuiCallbacks(TerminalCallbacks):  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise ImportError(_INSTALL_HINT) from _IMPORT_ERROR
