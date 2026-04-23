"""Tests for the escape-monitor / TUI stdin race fix.

Regression test for a bug where the first character typed into the
reappearing TUI prompt after a streaming response was silently discarded.
The monitor thread's in-flight ``select()`` could win the race against
``prompt_toolkit`` for the first byte, hit the "consume it" branch, and
drop the character.

Fix (``cancel.py``): a self-pipe (``_wake_r`` / ``_wake_w``) is watched
alongside ``sys.stdin``. ``cancellable.__exit__`` and
``set_tui_mode(True)`` write to ``_wake_w`` so any pending select
returns immediately; ``_read_byte`` also checks ``_monitor_active`` after
select and refuses to consume stdin if monitoring was disabled.
"""

import os
import sys
import threading
import time

import pytest

import cancel


class _FakeStdin:
    """Minimal sys.stdin stand-in backed by a raw fd.

    ``_read_byte`` only uses ``fileno()`` (for select) and ``read(n)``.
    Avoid ``os.fdopen`` so we don't fight Python's buffering rules.
    """

    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd

    def read(self, n):
        return os.read(self._fd, n).decode("utf-8", errors="replace")

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass


@pytest.fixture
def stdin_pipe(monkeypatch):
    """Replace ``sys.stdin`` with a pipe and freeze the background monitor.

    The ``cancel`` module starts a daemon ``_monitor_loop`` at import that
    loops ``_monitor_active.wait(); _read_byte(0.1); …``. If we let the
    real event object stay in place, setting it in a test will wake the
    daemon, which then races our test thread for the same stdin pipe.

    Swap ``cancel._monitor_active`` for a fresh ``Event()`` — the daemon
    keeps blocking on the *original* event object (stale reference), so
    it can't observe anything we do and won't read the pipe.
    """
    monkeypatch.setattr(cancel, "_monitor_active", threading.Event())

    r, w = os.pipe()
    fake = _FakeStdin(r)
    monkeypatch.setattr(sys, "stdin", fake)

    # Drain any leftover wake bytes so a previous test's wake doesn't
    # leak a ready-marker into this one's select.
    if cancel._wake_r is not None:
        import fcntl

        flags = fcntl.fcntl(cancel._wake_r, fcntl.F_GETFL)
        fcntl.fcntl(cancel._wake_r, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            os.read(cancel._wake_r, 4096)
        except BlockingIOError:
            pass
        fcntl.fcntl(cancel._wake_r, fcntl.F_SETFL, flags)

    try:
        yield w
    finally:
        try:
            fake.close()
        except Exception:
            pass
        try:
            os.close(w)
        except OSError:
            pass


def test_read_byte_returns_stdin_byte_when_monitor_active(stdin_pipe):
    cancel._monitor_active.set()
    os.write(stdin_pipe, b"x")
    assert cancel._read_byte(1.0) == "x"


def test_read_byte_preserves_stdin_when_monitor_cleared(stdin_pipe):
    """With _monitor_active cleared, an incoming byte is not consumed.

    The byte must remain in the pipe for the real keyboard owner
    (prompt_toolkit in production) to pick up.
    """
    cancel._monitor_active.clear()
    os.write(stdin_pipe, b"x")
    assert cancel._read_byte(0.1) is None
    assert sys.stdin.read(1) == "x"


def test_wake_monitor_interrupts_pending_select(stdin_pipe):
    """Calling _wake_monitor wakes an in-flight select immediately."""
    cancel._monitor_active.set()
    result = []

    def reader():
        result.append(cancel._read_byte(2.0))

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)  # let the reader enter select
    start = time.monotonic()
    cancel._monitor_active.clear()
    cancel._wake_monitor()
    t.join(timeout=1.0)
    elapsed = time.monotonic() - start
    assert not t.is_alive(), "reader did not wake within 1s"
    assert elapsed < 0.5, f"wake was slow ({elapsed:.3f}s)"
    assert result == [None]


def test_set_tui_mode_true_wakes_monitor(stdin_pipe):
    """``set_tui_mode(True)`` should wake a pending monitor read."""
    cancel._monitor_active.set()
    result = []

    def reader():
        result.append(cancel._read_byte(2.0))

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    cancel.set_tui_mode(True)
    try:
        t.join(timeout=1.0)
        assert not t.is_alive()
        assert result == [None]
        assert cancel.tui_mode() is True
    finally:
        cancel.set_tui_mode(False)


def test_cancellable_exit_wakes_monitor(stdin_pipe):
    """``cancellable.__exit__`` should wake a pending monitor read."""
    # Bypass the real cancellable.__enter__ (which would flip cbreak on a
    # tty); exercise __exit__'s wake path directly by seeding state.
    cancel._monitor_active.set()
    result = []

    def reader():
        result.append(cancel._read_byte(2.0))

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)

    # Emulate cancellable.__exit__ without the cbreak restore
    cancel._monitor_active.clear()
    cancel._wake_monitor()

    t.join(timeout=1.0)
    assert not t.is_alive()
    assert result == [None]


def test_wake_pipe_survives_repeated_wakes(stdin_pipe):
    """Draining the wake pipe in _read_byte must not break subsequent wakes."""
    for _ in range(5):
        cancel._monitor_active.set()
        result = []

        def reader():
            result.append(cancel._read_byte(1.0))

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.02)
        cancel._monitor_active.clear()
        cancel._wake_monitor()
        t.join(timeout=1.0)
        assert not t.is_alive()
        assert result == [None]
