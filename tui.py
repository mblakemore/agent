"""
prompt_toolkit-based TUI front-end for the interactive agent.

Optional feature — `prompt_toolkit` is not installed by default. When
`--tui` is passed but the package is missing, `TuiSession()` raises a
clean ImportError with an install hint. The rest of the agent continues
to work without it.

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

import os
from pathlib import Path
from typing import Callable

import cancel
from callbacks import TerminalCallbacks


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

_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help",    "show available commands"),
    ("/clear",   "clear conversation history"),
    ("/context", "show current context usage"),
    ("/model",   "pick a different model"),
    ("/verbose", "toggle compact/full tool output"),
    ("/tools",   "show recent tool calls"),
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
            "bottom-toolbar":                     f"bg:{_VIOLET_HEX} #ffffff",
            "bottom-toolbar.cwd":                 f"bg:{_VIOLET_HEX} {_SKY_HEX} bold",
            "bottom-toolbar.sep":                 f"bg:{_VIOLET_HEX} #707070",
            "bottom-toolbar.model":               f"bg:{_VIOLET_HEX} {_MINT_HEX}",
            "bottom-toolbar.msgs":                f"bg:{_VIOLET_HEX} #ffffff",
            "bottom-toolbar.ctx":                 f"bg:{_VIOLET_HEX} {_AMBER_HEX}",
            "bottom-toolbar.verbose-on":          f"bg:{_VIOLET_HEX} {_MINT_HEX} bold",
            "bottom-toolbar.verbose-off":         f"bg:{_VIOLET_HEX} #909090",
            "completion-menu.completion":         f"bg:{_VIOLET_HEX} #ffffff",
            "completion-menu.completion.current": f"bg:{_MINT_HEX} #000000 bold",
        })


    class LlmboxCompleter(Completer):
        """Slash-command + @path completer."""

        def __init__(self) -> None:
            self._path = PathCompleter(expanduser=True)

        def get_completions(self, document: Document, complete_event):
            text = document.text_before_cursor

            # Slash command at start of line — match prefix and offer all.
            if text.startswith("/") and " " not in text:
                for cmd, desc in _SLASH_COMMANDS:
                    if cmd.startswith(text):
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


    def _build_key_bindings() -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _(event) -> None:
            event.current_buffer.validate_and_handle()

        @kb.add("c-n")
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        return kb


    class TuiSession:
        """PromptSession wrapper with live bottom toolbar.

        Holds references to mutable loop state so the toolbar callback
        reflects the current model, message count, ctx% and verbose
        state on every keystroke without needing explicit refreshes.
        """

        def __init__(
            self,
            *,
            history: list,
            summary_state: dict,
            config: dict,
            ctx_size: int,
            cb: TerminalCallbacks,
            estimate_tokens: Callable[[dict], int],
        ) -> None:
            self.history = history
            self.summary_state = summary_state
            self.config = config
            self.ctx_size = ctx_size
            self.cb = cb
            self._estimate_tokens = estimate_tokens

            self._session: PromptSession = PromptSession(
                message=[("class:prompt", "\nYou: ")],
                multiline=False,
                key_bindings=_build_key_bindings(),
                completer=LlmboxCompleter(),
                complete_while_typing=False,
                bottom_toolbar=self._toolbar,
                style=_build_style(),
                color_depth=ColorDepth.TRUE_COLOR,
                enable_history_search=True,
                history=InMemoryHistory(),
            )

        # -- public API ------------------------------------------------

        def prompt(self) -> str:
            """Read one line from the user. Returns stripped text.

            TUI mode is flipped on only while the prompt is active so the
            cbreak-based double-escape monitor can still intercept cancels
            during streaming (between prompts).
            """
            cancel.set_tui_mode(True)
            try:
                return self._session.prompt().strip()
            finally:
                cancel.set_tui_mode(False)

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
            body = sum(self._estimate_tokens(m) for m in self.history) if self.history else 0
            summary = self.summary_state.get("text") or ""
            if summary:
                body += self._estimate_tokens({"role": "system", "content": summary})
            return min(1.0, body / self.ctx_size)

        def _toolbar(self):
            cwd = Path(os.getcwd()).name or "/"
            model = self.config.get("llm", {}).get("model", "?")
            msgs = len(self.history)
            pct = self._ctx_pct() * 100.0
            verbose = bool(getattr(self.cb, "verbose", False))
            vstate = "verbose on" if verbose else "verbose off"
            vcls = "bottom-toolbar.verbose-on" if verbose else "bottom-toolbar.verbose-off"

            left = (
                f'<style fg="{_SKY_HEX}" bg="{_VIOLET_HEX}"><b> {cwd} </b></style>'
                f'<style fg="#707070" bg="{_VIOLET_HEX}"> | </style>'
                f'<style fg="{_MINT_HEX}" bg="{_VIOLET_HEX}"> {model} </style>'
                f'<style fg="#707070" bg="{_VIOLET_HEX}"> | </style>'
                f'<style fg="#ffffff" bg="{_VIOLET_HEX}"> {msgs} msgs </style>'
                f'<style fg="#707070" bg="{_VIOLET_HEX}"> | </style>'
                f'<style fg="{_AMBER_HEX}" bg="{_VIOLET_HEX}"> ctx ~{pct:.0f}% </style>'
                f'<style fg="#707070" bg="{_VIOLET_HEX}"> | </style>'
                f'<style fg="{_MINT_HEX if verbose else "#909090"}" bg="{_VIOLET_HEX}"> {vstate} </style>'
            )

            # Pad to terminal width so the bar spans the screen.
            try:
                width = os.get_terminal_size().columns
            except OSError:
                width = 80
            visible_len = len(f" {cwd}  |  {model}  |  {msgs} msgs  |  ctx ~{pct:.0f}%  |  {vstate} ")
            pad = max(0, width - visible_len)
            return HTML(left + f'<style bg="{_VIOLET_HEX}">{" " * pad}</style>')


    class TuiCallbacks(TerminalCallbacks):
        """TerminalCallbacks with a TUI-aware session_start banner.

        Inherits compaction, tool_history recording, streaming de-dup,
        and /tools rendering unchanged — D12 invariants hold because
        the raw result_str path is untouched.
        """

        def __init__(self, tui_session: "TuiSession", *, verbose: bool = False) -> None:
            super().__init__(verbose=verbose)
            self.tui = tui_session
            self.tui.set_cb(self)

        def on_session_start(self, info: dict) -> None:
            super().on_session_start(info)
            self._note("[TUI mode: Enter submit · Ctrl+N newline · ↑/↓ history · Escape-Escape cancel]")


else:  # prompt_toolkit not installed — provide stubs that fail clearly

    _INSTALL_HINT = (
        "--tui requires the optional `prompt_toolkit` package. "
        "Install it with:  pip install prompt_toolkit"
    )


    class TuiSession:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise ImportError(_INSTALL_HINT) from _IMPORT_ERROR


    class TuiCallbacks(TerminalCallbacks):  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise ImportError(_INSTALL_HINT) from _IMPORT_ERROR
