"""exec_command output collapses to a terse OK in the console once the command
ran (non-verbose). Only tool-level errors (is_error / "Error:" results), other
tools, and /verbose still print in full. A non-zero exit code is NOT an error —
a `cmd && other` chain whose trailing part fails still collapses to OK."""

import callbacks


def _cb(verbose=False):
    cb = callbacks.TerminalCallbacks(verbose=verbose)
    lines = []
    cb._print = lambda text="", end="\n": lines.append(text)
    return cb, lines


# ── on_tool_result collapse ─────────────────────────────────────────────────

def test_success_collapses_to_ok():
    cb, lines = _cb()
    cb.on_tool_result("exec_command", {"command": "date"},
                      "[session: s] exit=0\nThu Jun 19", is_error=False)
    blob = "\n".join(lines)
    assert "OK" in blob
    assert "Result:" not in blob
    assert "Thu Jun 19" not in blob          # raw output suppressed in console


def test_nonzero_exit_still_collapses():
    # `uname -a && cat /missing` → exit=1 but the run is fine; not is_error.
    cb, lines = _cb()
    cb.on_tool_result("exec_command", {"command": "uname -a && cat /missing"},
                      "[session: s] exit=1\nLinux\ncat: /missing: No such file", is_error=False)
    blob = "\n".join(lines)
    assert "OK" in blob
    assert "Result:" not in blob
    assert "No such file" not in blob        # benign sub-command error suppressed


def test_tool_level_error_shows_result():
    # is_error=True (result starts with "Error:", e.g. no usable bash) → full.
    cb, lines = _cb()
    cb.on_tool_result("exec_command", {"command": "date"},
                      "Error: no usable bash found. Install Git for Windows ...",
                      is_error=True)
    blob = "\n".join(lines)
    assert "Result:" in blob
    assert "no usable bash" in blob


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
