"""Tests for the /alias helper (python detection + alias install)."""

import os

import alias_setup as A


# ── detect_python_cmd ───────────────────────────────────────────────────────

def test_detect_prefers_matching_name_on_path(monkeypatch):
    monkeypatch.setattr(A.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(A.shutil, "which", lambda n: f"/usr/bin/{n}" if n == "python3" else None)
    assert A.detect_python_cmd() == "python3"


def test_detect_skips_windows_store_stub(monkeypatch):
    monkeypatch.setattr(A.sys, "executable", r"C:\Python\python.exe")
    # 'python' resolves only to the Store stub; 'python3' not on PATH.
    def which(n):
        if n == "python":
            return r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe"
        return None
    monkeypatch.setattr(A.shutil, "which", which)
    # Neither usable name → fall back to the absolute interpreter path.
    assert A.detect_python_cmd() == r"C:\Python\python.exe"


def test_detect_falls_back_to_sys_executable(monkeypatch):
    monkeypatch.setattr(A.sys, "executable", "/opt/py/bin/python3.14")
    monkeypatch.setattr(A.shutil, "which", lambda n: None)
    assert A.detect_python_cmd() == "/opt/py/bin/python3.14"


# ── build_alias_commands ────────────────────────────────────────────────────

def test_build_alias_bash_uses_forward_slashes():
    cmds = A.build_alias_commands("python", r"C:\Users\me\agent\agent.py")
    assert cmds["bash"] == "alias agent='python \"C:/Users/me/agent/agent.py\"'"


def test_build_alias_powershell_and_cmd():
    cmds = A.build_alias_commands("python3", "/home/me/agent/agent.py")
    assert "function agent" in cmds["powershell"]
    assert "/home/me/agent/agent.py" in cmds["powershell"]
    assert cmds["cmd"].startswith("doskey agent=")


# ── current_shell_kind / rc_file_for ────────────────────────────────────────

def test_shell_kind_gitbash(monkeypatch):
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    assert A.current_shell_kind() == "gitbash"


def test_shell_kind_zsh(monkeypatch):
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.setattr(A.os, "name", "posix")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert A.current_shell_kind() == "zsh"


def test_shell_kind_windows(monkeypatch):
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.setattr(A.os, "name", "nt")
    assert A.current_shell_kind() == "windows"


def test_rc_file_for(monkeypatch, tmp_path):
    monkeypatch.setattr(A.os.path, "expanduser", lambda p: str(tmp_path))
    assert A.rc_file_for("gitbash").endswith(".bashrc")
    assert A.rc_file_for("zsh").endswith(".zshrc")
    assert A.rc_file_for("windows") is None


# ── install_alias_block ─────────────────────────────────────────────────────

def test_install_adds_then_unchanged_then_updated(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text("export PATH=/x\n")
    line = "alias agent='python \"/a/agent.py\"'"

    assert A.install_alias_block(str(rc), line) == "added"
    body = rc.read_text()
    assert "export PATH=/x" in body          # pre-existing content preserved
    assert line in body
    assert body.count(A._MARKER) == 1

    assert A.install_alias_block(str(rc), line) == "unchanged"
    assert rc.read_text().count(A._MARKER) == 1  # no duplicate block

    line2 = "alias agent='python3 \"/b/agent.py\"'"
    assert A.install_alias_block(str(rc), line2) == "updated"
    body2 = rc.read_text()
    assert line2 in body2 and line not in body2  # old line replaced, not stacked
    assert body2.count(A._MARKER) == 1
    assert "export PATH=/x" in body2


# ── /alias command integration ──────────────────────────────────────────────

def test_cmd_alias_dispatches_and_writes_rc(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import commands

    monkeypatch.setenv("MSYSTEM", "MINGW64")  # force gitbash → ~/.bashrc
    monkeypatch.setattr(A.os.path, "expanduser",
                        lambda p: str(tmp_path) if p == "~" else os.path.expanduser(p))
    ctx = SimpleNamespace(cb=MagicMock(), log=MagicMock())

    handled = commands.handle_command("/alias", ctx)

    assert handled is True
    rc = tmp_path / ".bashrc"
    assert rc.exists()
    assert "alias agent=" in rc.read_text()
