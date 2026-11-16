"""The always-live prompt: a thread that reads the keyboard while the agent works
(agent.py's live-input mode, `interactive.live_input`).

Each entered line goes on the monitor bus as ("user", text) and the main loop, the one
consumer, delivers it at the next natural boundary. Two control values never ride the bus
as text: EXIT_MARK ends the session (an exit line typed mid-turn must not reach the model)
and BORROW_MARK parks the thread while an interactive slash command owns the keyboard. A
process-wide patch_stdout held by the thread routes streaming and tool output above the
input line.
"""
import sys
import threading
import time
from contextlib import contextmanager

import theme

EXIT_MARK = "\x00__LIVE_INPUT_EXIT__"
BORROW_MARK = "\x00__TERMINAL_BORROW__"


class LiveInput:
    """One live prompt. `start()` runs it on a daemon thread; `stop()` + `join()` end it;
    `borrowed_terminal()` suspends it around input()-style IO. `recorder` is the optional
    transcript proxy that mirrors what the prompt accepts; `cancel` stops a running turn
    so an exit is prompt rather than waiting out a tool burst."""

    def __init__(self, tui_session, bus, log, *, recorder=None, cancel=None, patch_stdout=None):
        self.session = tui_session
        self.bus = bus
        self.log = log
        self.recorder = recorder
        self.cancel = cancel or (lambda: None)
        if patch_stdout is None:
            from prompt_toolkit.patch_stdout import patch_stdout
        self._patch_stdout = patch_stdout
        self._stop = threading.Event()
        self._borrow_ack = threading.Event()
        self._borrow_release = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="live-input", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    @property
    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    @contextmanager
    def borrowed_terminal(self):
        """Suspend the live prompt while the body runs input()-style IO.

        Retries the interrupt briefly — the common dispatch pattern (user submits
        '/model', main dequeues it instantly) races the input thread's NEXT prompt
        starting up, so the first attempt often finds no running app. Degrades to
        running the command anyway if no prompt appears within ~0.5s (no worse than
        the pre-borrow behavior). Half-typed text in the interrupted prompt is
        preserved via TuiSession._resume_default.
        """
        self._borrow_ack.clear()
        self._borrow_release.clear()
        borrowed = False
        for _ in range(10):
            if self.session.interrupt_prompt(BORROW_MARK):
                borrowed = True
                break
            time.sleep(0.05)
        if borrowed:
            self._borrow_ack.wait(timeout=2.0)
        try:
            yield
        finally:
            self._borrow_release.set()

    def run(self):
        """The thread body: prompt, classify, enqueue; never dies silently."""
        recorder = self.recorder
        try:
            with self._patch_stdout(raw=True):
                if recorder is not None:
                    # keep recording: forward to the proxy, which prints above the prompt
                    recorder.target, sys.stdout = sys.stdout, recorder
                while not self._stop.is_set():
                    try:
                        line = self.session.prompt_line()
                    except (EOFError, KeyboardInterrupt):
                        self.bus.put_user(EXIT_MARK)
                        return
                    if recorder is not None and line != BORROW_MARK:
                        # prompt_toolkit paints the accepted line itself
                        recorder.transcript.write(
                            "\n" + theme.c(theme.MINT, "You: ") + line + "\n")
                    if line == BORROW_MARK:
                        # Main thread borrowed the terminal for an interactive command —
                        # park (don't repaint a prompt over its input()) until released.
                        self._borrow_ack.set()
                        self._borrow_release.wait()
                        continue
                    if line:
                        # An exit line is CONTROL, not conversation: it must never ride the
                        # bus as text (mid-turn the btw-drain would frame it and hand it to
                        # the model). Deliver the mark instead, cancel any running turn so
                        # the exit is prompt rather than waiting out a long tool burst, and
                        # end this thread HERE — before the loop repaints a fresh prompt +
                        # toolbar the main thread is about to tear down.
                        if line.lower() in ("exit", "quit"):
                            self.cancel()
                            self.bus.put_user(EXIT_MARK)
                            return
                        self.bus.put_user(line)
        except Exception as e:  # never let the input thread die silently
            self.log.error("live-input thread crashed: %s", e)
            self.bus.put_user(EXIT_MARK)
        finally:
            if recorder is not None:
                recorder.target = recorder.real
