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
from pathlib import Path
from typing import Callable

import cancel
from callbacks import TerminalCallbacks


# Thread-local flag: True on a thread while its TuiSession.prompt() is
# actively blocked reading user input. Read from TuiCallbacks._print so
# that a background-thread print (e.g. on_summary_ready from the async
# summarizer) can wrap itself in patch_stdout only when it would otherwise
# corrupt the rendered prompt. If a hook fires on a thread that never
# entered prompt(), the flag is False and no wrapping is needed — that
# thread doesn't own the terminal anyway.
_prompt_active = threading.local()


def _prompt_is_active() -> bool:
    return getattr(_prompt_active, "on", False)


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
            # Toolbar ctx% cache. Key is (len(history), len(summary_text)) —
            # cheap to compute, catches both "new message appended" and
            # "summary updated in place". The rare case where the summary
            # is rewritten to the same length shows a stale percentage for
            # exactly one keystroke — acceptable staleness.
            self._ctx_cache_key: tuple[int, int] | None = None
            self._ctx_cache_val: float = 0.0

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
            during streaming (between prompts). The _prompt_active flag
            is set in lockstep so TuiCallbacks._print can route through
            patch_stdout while a prompt is displayed.
            """
            _prompt_active.on = True
            cancel.set_tui_mode(True)
            try:
                return self._session.prompt().strip()
            finally:
                cancel.set_tui_mode(False)
                _prompt_active.on = False

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

        def _toolbar(self):
            cwd = Path(os.getcwd()).name or "/"
            model = self.config.get("llm", {}).get("model", "?")
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

            # Pad to terminal width so the bar spans the screen.
            try:
                width = os.get_terminal_size().columns
            except OSError:
                width = 80
            visible_base = f" {cwd}  │  {model}  │  {msgs} msgs  │  {ctx_label} "
            visible_len = len(visible_base) + (len("  │   verbose ") if verbose else 0)
            pad = max(0, width - visible_len)

            # Second line: full working directory with $HOME collapsed to ~.
            full_cwd = os.getcwd()
            home = os.path.expanduser("~")
            if full_cwd == home or full_cwd.startswith(home + os.sep):
                full_cwd = "~" + full_cwd[len(home):]
            cwd_text = f" {full_cwd} "
            cwd_pad = max(0, width - len(cwd_text))
            second = (
                f'<style fg="{_BAR_FG_HEX}" bg="{_BAR_BG2_HEX}">{html.escape(cwd_text)}</style>'
                f'<style bg="{_BAR_BG2_HEX}">{" " * cwd_pad}</style>'
            )

            return HTML(
                left
                + f'<style bg="{_BAR_BG_HEX}">{" " * pad}</style>'
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

        def _print(self, text: str = "", end: str = "\n") -> None:
            if _prompt_is_active():
                from prompt_toolkit.patch_stdout import patch_stdout
                with patch_stdout(raw=True):
                    print(text, end=end, flush=True)
            else:
                print(text, end=end, flush=True)

        def on_session_start(self, info: dict) -> None:
            super().on_session_start(info)
            self._note("[TUI mode: Enter submit · Ctrl+N newline · ↑/↓ history · Escape-Escape cancel]")


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
