"""
Double-escape cancellation for streaming responses.

Two Escape presses within 400ms aborts the current streaming operation.
Single escapes and ANSI sequences (arrow keys, etc.) are ignored.

When a richer TUI host owns the keyboard (see set_tui_mode), the cbreak
capture + monitor are suppressed; the host is expected to call
request_cancel() on its own keybinding.
"""

import atexit
import io
import os
import select
import sys
import termios
import threading
import time
import tty


class CancelledError(Exception):
    """Raised when the user cancels a streaming operation."""
    pass


# Global state for terminal restoration
_cancel_event = threading.Event()
_monitor_active = threading.Event()
_original_termios = None
_tui_mode = False

# Self-pipe for waking the escape-monitor thread out of a pending select() when
# monitoring is disabled (end of a cancellable region, or a TUI host taking
# over the keyboard). Without this, the monitor can sit in a select() with up
# to 100ms of timeout remaining AFTER _monitor_active is cleared — and if the
# user starts typing into the reappearing TUI prompt in that window, the
# monitor's select wins the race and silently swallows the first character
# (line-166 "consume it" path). Writing a byte to _wake_w makes the select
# return immediately; the reader drains the pipe and returns None without
# consuming stdin.
try:
    _wake_r, _wake_w = os.pipe()
except OSError:
    # Non-POSIX or restricted environment — fall back to race-prone behaviour.
    _wake_r, _wake_w = None, None


def _wake_monitor():
    """Unblock a pending _read_byte() select. Safe to call any time."""
    if _wake_w is None:
        return
    try:
        os.write(_wake_w, b"!")
    except OSError:
        pass


def is_cancelled():
    """Check whether cancellation has been requested."""
    return _cancel_event.is_set()


def check_cancelled():
    """Raise CancelledError if cancellation has been requested."""
    if _cancel_event.is_set():
        raise CancelledError()


def reset():
    """Clear the cancellation flag."""
    _cancel_event.clear()


def request_cancel():
    """Set the cancel flag from an external source (e.g. TUI keybinding)."""
    _cancel_event.set()


def set_tui_mode(enabled):
    """Enable or disable TUI mode.

    When enabled, the cancellable context manager skips cbreak capture and
    the background escape monitor — a prompt_toolkit host (or equivalent)
    is expected to own the keyboard and call request_cancel() itself.
    """
    global _tui_mode
    _tui_mode = bool(enabled)
    if _tui_mode:
        _monitor_active.clear()
        _wake_monitor()


def tui_mode():
    """Return True when TUI mode is active."""
    return _tui_mode


class cbreak_mode:
    """Context manager that switches the terminal to cbreak mode.

    Individual keypresses become readable, Ctrl+C still raises KeyboardInterrupt.
    Original terminal settings are restored on exit.
    """

    def __init__(self):
        self.saved = None

    def __enter__(self):
        global _original_termios
        if not sys.stdin.isatty():
            return self
        self.saved = termios.tcgetattr(sys.stdin)
        _original_termios = self.saved
        tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        global _original_termios
        if self.saved is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.saved)
            _original_termios = None
        return False


def _read_byte(timeout):
    """Read a single byte from stdin with timeout. Returns byte or None.

    Watches a wake pipe alongside stdin. If the pipe fires or _monitor_active
    was cleared while we were in select, returns None without consuming any
    stdin byte — lets the real keyboard owner (prompt_toolkit, etc.) pick up
    the next keypress.
    """
    try:
        if not hasattr(sys.stdin, 'fileno'):
            return None
        watch_fds = [sys.stdin]
        if _wake_r is not None:
            watch_fds.append(_wake_r)
        ready, _, _ = select.select(watch_fds, [], [], timeout)
        if sys.stdin in ready and _monitor_active.is_set():
            return sys.stdin.read(1)
        if _wake_r is not None and _wake_r in ready:
            try:
                os.read(_wake_r, 4096)
            except OSError:
                pass
        return None
    except (io.UnsupportedOperation, ValueError, select.error):
        return None


def _consume_ansi_sequence():
    """Consume the remainder of an ANSI escape sequence after ESC."""
    ch = _read_byte(0.05)
    if ch is None:
        return  # bare ESC, not an ANSI sequence
    if ch == b'[':
        # CSI sequence: read until a letter (@ through ~)
        while True:
            ch = _read_byte(0.05)
            if ch is None or (b'\x40' <= ch <= b'\x7e'):
                break
    elif ch == b'O':
        # SS3 sequence (e.g. arrow keys in some terminals): one more byte
        _read_byte(0.05)
    # else: two-char escape sequence, already consumed


def _monitor_loop():
    """Background thread that watches for double-escape."""
    last_esc_time = None

    while True:
        _monitor_active.wait()

        ch = _read_byte(0.1)
        if ch is None:
            continue

        if ch == b'\x1b':
            # Check if this is the start of an ANSI sequence
            next_ch = _read_byte(0.05)
            if next_ch is not None:
                # ANSI sequence — consume it and ignore
                if next_ch == b'[':
                    while True:
                        c = _read_byte(0.05)
                        if c is None or (b'\x40' <= c <= b'\x7e'):
                            break
                    # Note: this was duplicated in _consume_ansi_sequence,
                    # but we keep the logic for the loop's primary read.
                elif next_ch == b'O':
                    _read_byte(0.05)
                # else: two-char sequence, consumed
                continue

            # Bare escape — check for double-escape
            now = time.monotonic()
            if last_esc_time is not None and (now - last_esc_time) < 0.4:
                _cancel_event.set()
                last_esc_time = None
            else:
                last_esc_time = now
        else:
            # Non-escape keypress — consume it (prevent buffer leakage)
            last_esc_time = None
# Start the monitor thread as a daemon
_monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
_monitor_thread.start()


class cancellable:
    """Context manager that enables double-escape cancellation.

    Combines reset + cbreak mode + monitor activation.
    Gracefully degrades when stdin isn't a terminal.
    """

    def __init__(self):
        self._cbreak = None

    def __enter__(self):
        reset()
        if _tui_mode:
            # A TUI host owns the keyboard; it will call request_cancel().
            return self
        if sys.stdin.isatty():
            self._cbreak = cbreak_mode()
            self._cbreak.__enter__()
            _monitor_active.set()
        return self

    def __exit__(self, *exc):
        _monitor_active.clear()
        _wake_monitor()
        if self._cbreak is not None:
            self._cbreak.__exit__(*exc)
            self._cbreak = None
        return False


def _restore_terminal():
    """atexit handler — restore terminal settings if we crashed in cbreak mode."""
    global _original_termios
    if _original_termios is not None:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _original_termios)
            # Use a try-except here as well because we might be in a weird state
        except Exception:
            pass


atexit.register(_restore_terminal)
