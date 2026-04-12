"""
StreamStatus — visual feedback for model streaming.

Phase 1 (waiting):   Aurora-pulsed braille spinner with elapsed time.
Phase 2 (streaming): Live token count + t/s in the terminal title bar.
Phase 3 (done):      Dim summary line, terminal title reset.
"""

import sys
import threading
import time

import theme

RESET = theme.RESET
CLEAR_LINE = theme.CLEAR_LINE

_BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _interactive():
    """Cheap check: only emit cursor controls / title updates on a real TTY
    with color enabled. Piping to a file or NO_COLOR suppresses them."""
    return not theme._no_color()


class StreamStatus:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._start_time = None
        self._first_token_time = None
        self._token_count = 0
        self._prefix = ""
        self._interactive = _interactive()

    # -- Phase 1: spinner --------------------------------------------------

    def start(self, prefix=""):
        """Show an animated spinner with elapsed time.

        Any leading newlines in prefix are printed once upfront so the
        spinner thread can use \\r to overwrite the same line.
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
            # Non-interactive: just print the prefix once so downstream output
            # still has its header.
            if self._prefix:
                sys.stdout.write(self._prefix)
                sys.stdout.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            elapsed = time.monotonic() - self._start_time
            frame = _BRAILLE[i % len(_BRAILLE)]
            color = theme.pulse_escape(elapsed)
            reset = RESET if color else ""
            sys.stdout.write(
                f"{CLEAR_LINE}{self._prefix}{color}{frame}{reset} {theme.dim(f'{elapsed:.1f}s')}"
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
        if self._interactive:
            sys.stdout.write(f"{CLEAR_LINE}{self._prefix}")
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

        if self._interactive:
            sys.stdout.write("\033]0;\007")
            sys.stdout.flush()

        if self._start_time is None:
            return

        total = time.monotonic() - self._start_time
        if self._token_count > 0:
            tps = self._token_count / total if total > 0 else 0
              _emit("on_notice", "info", theme.dim(f"[{self._token_count} tokens \u00b7 {total:.1f}s \u00b7 {tps:.1f} t/s]"))
        elif self._interactive:
            sys.stdout.write(CLEAR_LINE)
            sys.stdout.flush()
