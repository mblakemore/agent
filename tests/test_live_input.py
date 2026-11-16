"""live_input.py: the prompt thread's control values and its never-silent exit."""
import contextlib
import logging
import threading

import live_input


class _Bus:
    def __init__(self):
        self.items = []

    def put_user(self, text):
        self.items.append(text)


class _Session:
    """Scripted prompt: each prompt_line() pops the next value; an exception ends it."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.interrupts = []

    def prompt_line(self):
        v = self.lines.pop(0)
        if isinstance(v, BaseException):
            raise v
        return v

    def interrupt_prompt(self, result):
        self.interrupts.append(result)
        return True


def _run(lines, **kw):
    bus = _Bus()
    li = live_input.LiveInput(_Session(lines), bus, logging.getLogger("t"),
                              patch_stdout=lambda raw: contextlib.nullcontext(), **kw)
    li.run()
    return bus.items


def test_lines_ride_the_bus_and_exit_is_a_mark_not_text():
    cancelled = []
    items = _run(["hello", "", "Quit", "never read"], cancel=lambda: cancelled.append(1))
    assert items == ["hello", live_input.EXIT_MARK]      # the empty line is dropped
    assert cancelled == [1]


def test_eof_and_crash_both_end_with_the_exit_mark():
    assert _run(["a", EOFError()]) == ["a", live_input.EXIT_MARK]
    assert _run([RuntimeError("boom")]) == [live_input.EXIT_MARK]


def test_borrow_parks_the_thread_until_released():
    bus = _Bus()
    sess = _Session([live_input.BORROW_MARK, "after", EOFError()])
    li = live_input.LiveInput(sess, bus, logging.getLogger("t"),
                              patch_stdout=lambda raw: contextlib.nullcontext())
    li.start()
    assert li._borrow_ack.wait(timeout=2)            # the thread saw the mark and parked
    assert bus.items == []
    with li.borrowed_terminal():
        assert bus.items == []                       # still parked while the body runs
    li.join(timeout=2)
    assert not li.alive
    assert bus.items == ["after", live_input.EXIT_MARK]
    assert sess.interrupts == [live_input.BORROW_MARK]


def test_stop_ends_the_loop_before_the_next_prompt():
    bus = _Bus()
    gate = threading.Event()

    class _Blocking(_Session):
        def prompt_line(self):
            gate.wait(timeout=2)
            return super().prompt_line()

    li = live_input.LiveInput(_Blocking(["late"]), bus, logging.getLogger("t"),
                              patch_stdout=lambda raw: contextlib.nullcontext())
    li.start()
    li.stop()
    gate.set()
    li.join(timeout=2)
    assert bus.items == ["late"] and not li.alive   # the line in flight is delivered, then it ends
