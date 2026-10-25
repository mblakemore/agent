"""transcript.py: the record a terminal resize reprints."""

import io

import theme
import transcript as T


def test_lines_carriage_returns_and_colors():
    t = T.Transcript()
    t.write("one\ntw")
    t.write("o\nspinner 1\rspinner 2\rdone\n")
    t.write(theme.c(theme.MINT, "green") + "\n")
    assert t.render(80).splitlines() == ["one", "two", "done", theme.c(theme.MINT, "green")]


def test_cursor_up_and_clear_removes_lines():
    """The boot lines erase themselves with cursor_up_clear before the banner."""
    t = T.Transcript()
    t.write("keep\nboot 1\nboot 2\n")
    t.write("\x1b[2A\x1b[0Jbanner\n")
    assert t.render(80) == "keep\nbanner\n"


def test_cursor_up_past_the_top_is_harmless():
    t = T.Transcript()
    t.write("\x1b[5A\x1b[0Jbanner\n")
    assert t.render(80) == "banner\n"


def test_a_partial_line_is_kept_without_a_newline():
    t = T.Transcript()
    t.write("a\nhalf")
    assert t.render(80) == "a\nhalf"


def test_fitted_rows_are_refitted_for_the_new_width():
    t = T.Transcript()
    long = "  → exec_command(command='ls -la', cwd='/a/very/long/working/directory/path/here')"
    narrow = theme.truncate_middle(long, 39)
    t.note_fit(narrow, long)
    t.note_fit(long, long)          # fitted uncut at the old width, still refitted
    t.write(narrow + "\n" + long + "\n")
    wide = t.render(200).splitlines()
    assert wide == [long, long]
    assert all(len(theme.strip_ansi(l)) <= 29 for l in t.render(30).splitlines())


def test_record_is_bounded():
    t = T.Transcript(max_lines=3)
    t.write("1\n2\n3\n4\n5\n")
    assert t.render(80) == "3\n4\n5\n"


def test_recorder_records_and_forwards():
    t = T.Transcript()
    out = io.StringIO()
    r = T.Recorder(t, out)
    print("hello", file=r)
    assert out.getvalue() == "hello\n" and t.render(80) == "hello\n"
    proxy = io.StringIO()
    r.target = proxy                # live input: forwards to patch_stdout's proxy
    r.write("x\n")
    assert proxy.getvalue() == "x\n" and r.real is out


def test_install_skips_a_non_terminal(monkeypatch):
    monkeypatch.setattr(T, "_current", None)
    assert T.install() is None      # pytest's captured stdout is not a tty
    assert T.current() is None


def test_tui_reflow_clears_and_reprints(monkeypatch):
    import sys
    import tui
    from types import SimpleNamespace
    t = T.Transcript()
    t.write("line one\n")
    real = io.StringIO()
    rec = T.Recorder(t, real)
    monkeypatch.setattr(sys, "stdout", rec)
    monkeypatch.setattr(T, "_current", t)
    sess = tui.TuiSession.__new__(tui.TuiSession)
    sess._session = SimpleNamespace(app=SimpleNamespace(
        output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=50))))
    sess._reflow()
    assert real.getvalue() == "\x1b[H\x1b[2J\x1b[3Jline one\n"
    assert t.render(80) == "line one\n"      # the reprint itself is not recorded
