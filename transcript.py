"""What the interactive session printed, kept so a terminal resize can reprint it.

A terminal never rewraps what has scrolled up, and the one-line status rows the
UI middle-truncates to the width stay cut for the old width. In live-input mode
``TuiSession`` reprints this record when the window size settles: clear the
screen and scrollback, write the record at the new width, let the prompt redraw.

``Recorder`` stands in for ``sys.stdout``: every write is recorded and passed on
to ``target`` (the real stdout, or prompt_toolkit's patch_stdout proxy while the
live prompt runs). prompt_toolkit's own screen painting goes to the real stdout
directly, so prompt redraws are never recorded.

The record is lines, not bytes: carriage returns overwrite the line, the
cursor-up-and-clear the boot lines use removes lines, and other cursor movement
is dropped. Colors are kept.
"""

from __future__ import annotations

import re
import sys
import threading
from collections import OrderedDict, deque

import theme

MAX_LINES = 5000          # reprinting more on every resize gets slow
_MAX_FITS = 5000

# CSI sequences: SGR (…m) is kept; cursor up (…A) and erase (…J / …K) are applied;
# anything else is dropped.
_CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


class Transcript:
    def __init__(self, max_lines: int = MAX_LINES):
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._cur = ""
        self._fits: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def note_fit(self, fitted: str, original: str) -> None:
        """``fitted`` is ``original`` middle-truncated to the width (possibly not
        cut at all); a reprint truncates ``original`` again for the new width."""
        with self._lock:
            self._fits[fitted] = original
            self._fits.move_to_end(fitted)
            while len(self._fits) > _MAX_FITS:
                self._fits.popitem(last=False)

    def write(self, text: str) -> None:
        with self._lock:
            pos = 0
            for m in _CSI.finditer(text):
                self._plain(text[pos:m.start()])
                args, op = m.group(1), m.group(2)
                if op == "m":
                    self._cur += m.group(0)
                elif op == "A":
                    for _ in range(int(args or 1)):
                        if not self._lines:
                            break
                        self._cur = self._lines.pop()
                elif op in "JK":
                    self._cur = ""      # the UI only clears from column 0
                pos = m.end()
            self._plain(text[pos:])

    def _plain(self, text: str) -> None:
        while text:
            nl = text.find("\n")
            seg, text = (text, "") if nl < 0 else (text[:nl], text[nl + 1:])
            if "\r" in seg:
                self._cur, seg = "", seg.rsplit("\r", 1)[1]
            self._cur += seg
            if nl < 0:
                break
            self._lines.append(self._cur)
            self._cur = ""

    def render(self, width: int) -> str:
        """The record as text for a terminal ``width`` columns wide."""
        with self._lock:
            lines = list(self._lines) + ([self._cur] if self._cur else [])
            fits = dict(self._fits)
        out = []
        for line in lines:
            original = fits.get(line)
            out.append(theme.truncate_middle(original, width - 1) if original is not None else line)
        return "\n".join(out) + ("\n" if not self._cur else "")


class Recorder:
    """A ``sys.stdout`` stand-in that records every write into ``transcript``."""

    def __init__(self, transcript: Transcript, target):
        self.transcript = transcript
        self.target = target
        self.real = target          # the terminal, for the reprint itself

    def write(self, text):
        self.transcript.write(text)
        return self.target.write(text)

    def flush(self):
        return self.target.flush()

    def __getattr__(self, name):
        return getattr(self.target, name)


_current: Transcript | None = None


def install() -> Recorder | None:
    """Start recording ``sys.stdout``. Returns the recorder, or None when stdout
    is not a terminal. prompt_toolkit's output must already be bound to the real
    stdout, or its screen painting would be recorded too."""
    global _current
    if isinstance(sys.stdout, Recorder):
        return sys.stdout
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return None
    try:
        from prompt_toolkit.application.current import get_app_session
        get_app_session().output       # created now, around the real stdout
    except Exception:  # noqa: BLE001 — no prompt_toolkit: nothing to keep apart
        pass
    _current = Transcript()
    rec = Recorder(_current, sys.stdout)
    sys.stdout = rec
    return rec


def note_fit(fitted: str, original: str) -> None:
    if _current is not None:
        _current.note_fit(fitted, original)


def current() -> Transcript | None:
    return _current
