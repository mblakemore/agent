"""
StreamStatus — visual feedback for model streaming.

Phase 1 (waiting):   Aurora-pulsed braille spinner with elapsed time.
Phase 2 (streaming): Live token count + t/s in the terminal title bar.
Phase 3 (done):      Dim summary line, terminal title reset.
"""

import shutil
import sys
import threading
import time

import theme

RESET = theme.RESET
CLEAR_LINE = theme.CLEAR_LINE

_BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ── Shared spinner state for prompt_toolkit hosts (live-input mode) ─────
# When a prompt_toolkit app owns the terminal the \r-redraw spinner is
# suppressed (see _interactive) — instead StreamStatus publishes its state
# here and a ticker thread invalidates the app ~10x/s so the TUI bottom
# toolbar can render an animated spinner segment (tui.TuiSession._toolbar
# reads get_active()). This is what restores visible progress feedback in
# concurrent live-input mode (defect #1 of plan/live-input-display-fixes.md).
_active_lock = threading.Lock()
_active = None            # {"label": str, "t0": float} | None
_ticker_running = False


def frame_at(elapsed):
    """Braille frame for a given elapsed time (10 fps cycle)."""
    return _BRAILLE[int(elapsed * 10) % len(_BRAILLE)]


def get_active():
    """Snapshot of the PT-hosted spinner state: (label, t0) or None."""
    with _active_lock:
        if _active is None:
            return None
        return (_active["label"], _active["t0"])


def _pt_app():
    """The running prompt_toolkit app visible from this thread, or None.

    Threads share the default AppSession, so the live-input thread's
    always-running prompt app is visible from the agent loop and from the
    ticker thread alike (probe-verified — see agent.py live-input notes).
    """
    try:
        from prompt_toolkit.application import get_app_or_none
        return get_app_or_none()
    except Exception:
        return None


def _set_active(label, t0):
    global _active, _ticker_running
    start_ticker = False
    with _active_lock:
        _active = {"label": label, "t0": t0}
        if not _ticker_running:
            _ticker_running = True
            start_ticker = True
    if start_ticker:
        threading.Thread(target=_ticker_loop, name="spinner-ticker",
                         daemon=True).start()


def _clear_active():
    global _active
    with _active_lock:
        _active = None


def _ticker_loop():
    """Animate the toolbar: invalidate the PT app while a spinner is active.

    Exit and ticker-start are serialized under _active_lock ("_active is
    None → _ticker_running = False" is atomic), so a clear-then-set race
    can never leave an active spinner without a ticker.
    """
    global _ticker_running
    while True:
        with _active_lock:
            if _active is None:
                _ticker_running = False
                return
        app = _pt_app()
        if app is not None and getattr(app, "is_running", False):
            try:
                app.invalidate()
            except Exception:
                pass
        time.sleep(0.1)


def _interactive():
    """Cheap check: only emit cursor controls / title updates on a real TTY
    with color enabled. Piping to a file or NO_COLOR suppresses them.

    Also suppressed while a prompt_toolkit application owns the terminal
    (concurrent live-input mode): its output is routed through patch_stdout,
    which renders whole lines above the live prompt and is incompatible with
    our \\r carriage-return spinner redraws — they collide and leave fragments
    like 'preparing tool calls ⠙ 11.1scycle:'. Falling back to non-interactive
    (print the prefix once, no redraw thread) renders cleanly through the proxy.
    In the default blocking mode no app is running during processing, so this
    is a no-op there.
    """
    if theme._no_color():
        return False
    try:
        from prompt_toolkit.application import get_app_or_none
        if get_app_or_none() is not None:
            return False
    except Exception:
        pass
    return True


class StreamStatus:
    def __init__(self, emit=None):
        self._stop = threading.Event()
        self._thread = None
        self._start_time = None
        self._first_token_time = None
        self._token_count = 0
        self._prefix = ""
        self._interactive = _interactive()
        self._tui_spin = False  # True when this instance published PT state
        self._emit = emit if emit is not None else lambda event, level, msg: print(msg)

    # -- Phase 1: spinner --------------------------------------------------

    def start(self, prefix="", pt_header=True):
        """Show an animated spinner with elapsed time.

        Any leading newlines in prefix are printed once upfront so the
        spinner thread can use \\r to overwrite the same line.

        pt_header: when a prompt_toolkit app owns the terminal, whether to
        also print the prefix as a one-shot scrollback header. The main
        response stream wants it (streamed lines follow the header); the
        per-tool spinner passes False — its header is printed by
        on_tool_start after the tool completes, and printing both
        duplicated every tool line in live-input mode.
        """
        stripped = prefix.lstrip("\n")
        leading = prefix[: len(prefix) - len(stripped)]
        if leading:
            sys.stdout.write(leading)
            sys.stdout.flush()
        self._prefix = stripped
        self._start_time = time.monotonic()
        self._stop.clear()
        if self._interactive:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            # A prompt_toolkit app owns the terminal: publish shared state so
            # the bottom toolbar renders the spinner (animated by the ticker).
            if _pt_app() is not None:
                self._tui_spin = True
                label = theme.strip_ansi(self._prefix).strip() or "working"
                _set_active(label, self._start_time)
                if not pt_header:
                    return
            # Non-interactive: just print the prefix once so downstream output
            # still has its header. On a real TTY that a prompt_toolkit app
            # owns, an over-width prefix would wrap across rows in the
            # scrollback — middle-truncate it. Piped output (NO_COLOR /
            # non-TTY) keeps the full line: log files want everything.
            if self._prefix:
                out = self._prefix
                if not theme._no_color():
                    cols = shutil.get_terminal_size((80, 24)).columns
                    out = theme.truncate_middle(out, cols - 1)
                sys.stdout.write(out + "\n")
                sys.stdout.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            elapsed = time.monotonic() - self._start_time
            frame = _BRAILLE[i % len(_BRAILLE)]
            color = theme.pulse_escape(elapsed)
            reset = RESET if color else ""
            elapsed_txt = f"{elapsed:.1f}s"
            # Re-query the width every frame (handles resize) and budget the
            # prefix so `prefix + frame + " " + elapsed` can never exceed
            # width-1. A wrapped spinner line defeats \r + CLEAR_LINE — they
            # only reach the last wrapped row, so every redraw scrolls one
            # duplicate line while the counter ticks. Middle truncation keeps
            # the head and tail of the line visible.
            cols = shutil.get_terminal_size((80, 24)).columns
            budget = max(8, cols - 1 - (2 + len(elapsed_txt)))
            prefix = theme.truncate_middle(self._prefix, budget)
            sys.stdout.write(
                f"{CLEAR_LINE}{prefix}{color}{frame}{reset} {theme.dim(elapsed_txt)}"
            )
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.1)

    # -- Phase 2: streaming tokens -----------------------------------------

    def first_token(self):
        """Stop the spinner and print the real header."""
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        self._first_token_time = time.monotonic()
        if self._tui_spin:
            # Keep the toolbar segment alive through streaming — the toolbar
            # appends the live tail of the in-progress line (cb.stream_tail),
            # so the label flips to a streaming indicator, elapsed continues.
            _set_active("streaming", self._start_time)
        if self._interactive:
            cols = shutil.get_terminal_size((80, 24)).columns
            sys.stdout.write(
                f"{CLEAR_LINE}{theme.truncate_middle(self._prefix, cols - 1)}"
            )
            sys.stdout.flush()
        # Non-interactive: prefix was already written in start()

    def count_token(self):
        """Increment token counter and update terminal title (throttled)."""
        self._token_count += 1
        if not self._interactive:
            return
        if self._token_count % 5 == 0:
            elapsed = time.monotonic() - (self._first_token_time or self._start_time)
            tps = self._token_count / elapsed if elapsed > 0 else 0
            sys.stdout.write(f"\033]0;{self._token_count} tokens \u00b7 {tps:.1f} t/s\007")
            sys.stdout.flush()

    # -- Phase 3: done -----------------------------------------------------

    def finish(self):
        """Reset terminal title and print summary stats."""
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        if self._tui_spin:
            self._tui_spin = False
            _clear_active()

        if self._interactive:
            sys.stdout.write("\033]0;\007")
            sys.stdout.flush()

        if self._start_time is None:
            return

        total = time.monotonic() - self._start_time
        if self._token_count > 0:
            tps = self._token_count / total if total > 0 else 0
            self._emit("on_notice", "info", theme.dim(f"[{self._token_count} tokens \u00b7 {total:.1f}s \u00b7 {tps:.1f} t/s]"))
        elif self._interactive:
            sys.stdout.write(CLEAR_LINE)
            sys.stdout.flush()
