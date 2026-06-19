"""Successful exec_command output collapses to a terse OK in the console
(non-verbose); failures, non-exec tools, and /verbose still print in full."""

import callbacks


def _cb(verbose=False):
    cb = callbacks.TerminalCallbacks(verbose=verbose)
    lines = []
    cb._print = lambda text="", end="\n": lines.append(text)
    return cb, lines


# ── _exec_succeeded ─────────────────────────────────────────────────────────

def test_exec_succeeded_exit0():
    assert callbacks._exec_succeeded("[session: x] exit=0\nhello") is True


def test_exec_succeeded_nonzero():
    assert callbacks._exec_succeeded("[session: x] exit=1\nboom") is False


def test_exec_succeeded_no_marker():
    assert callbacks._exec_succeeded("Command started in background. Poll ...") is False
    assert callbacks._exec_succeeded("") is False


# ── on_tool_result collapse ─────────────────────────────────────────────────

def test_success_collapses_to_ok():
    cb, lines = _cb()
    cb.on_tool_result("exec_command", {"command": "date"},
                      "[session: s] exit=0\nThu Jun 19", is_error=False)
    blob = "\n".join(lines)
    assert "OK" in blob
    assert "Result:" not in blob
    assert "Thu Jun 19" not in blob          # raw output suppressed in console


def test_failure_shows_full_result():
    cb, lines = _cb()
    cb.on_tool_result("exec_command", {"command": "cat /nope"},
                      "[session: s] exit=1\nNo such file", is_error=False)
    blob = "\n".join(lines)
    assert "Result:" in blob
    assert "No such file" in blob


def test_is_error_shows_result():
    cb, lines = _cb()
    cb.on_tool_result("exec_command", {"command": "x"},
                      "[session: s] exit=0\nweird", is_error=True)
    assert "Result:" in "\n".join(lines)     # is_error always shown


def test_verbose_shows_full_even_on_success():
    cb, lines = _cb(verbose=True)
    cb.on_tool_result("exec_command", {"command": "date"},
                      "[session: s] exit=0\nThu Jun 19", is_error=False)
    blob = "\n".join(lines)
    assert "Result:" in blob and "Thu Jun 19" in blob


def test_non_exec_tool_not_collapsed():
    cb, lines = _cb()
    cb.on_tool_result("read_file", {"path": "a.py"}, "file contents here", is_error=False)
    blob = "\n".join(lines)
    assert "Result:" in blob                 # other tools unchanged


def test_result_still_recorded_in_history_on_collapse():
    cb, _ = _cb()
    cb.on_tool_result("exec_command", {"command": "date"},
                      "[session: s] exit=0\nThu Jun 19", is_error=False)
    # The model/-tools view keeps the full result even though the console hid it.
    assert cb.tool_history[-1][2] == "[session: s] exit=0\nThu Jun 19"
