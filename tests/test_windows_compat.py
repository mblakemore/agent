"""Platform-simulation tests for the Windows (Git-Bash / C1) port.

This suite runs on Linux but exercises the *Windows* code paths by simulating
the platform differences (missing ``termios``/``tty``/``fcntl``, ``os.name ==
'nt'``). These tests are the only validation signal for the Windows branches
until a native ``windows-2022`` CI runner exists — a green Linux suite without
them proves only that POSIX did not regress, NOT that Windows works.
"""

import errno
import importlib
import os
import sys

import pytest


# ───────────────────────── cancel.py ─────────────────────────

def test_cancel_imports_without_termios():
    """cancel.py must import cleanly when termios/tty are unavailable, and
    fully disable the cbreak/monitor machinery (the Windows import path)."""
    import cancel
    blocked = ("termios", "tty")
    saved = {n: sys.modules.get(n) for n in blocked}
    try:
        for n in blocked:
            sys.modules[n] = None  # makes `import n` raise ImportError
        cancel = importlib.reload(cancel)
        assert cancel._POSIX_TTY is False
        assert cancel.termios is None and cancel.tty is None
        # Wake pipe disabled so select() is never called on Windows fds.
        assert cancel._wake_r is None and cancel._wake_w is None
        # _read_byte short-circuits without touching select.
        assert cancel._read_byte(0.0) is None
    finally:
        # Restore real modules and reload cancel back to POSIX state so later
        # tests in this process are unaffected.
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
        importlib.reload(cancel)


def test_cancellable_does_not_activate_monitor_on_non_posix(monkeypatch):
    """With no POSIX tty, cancellable() must not arm the cbreak monitor —
    otherwise select() on Windows fds would busy-loop a core."""
    import cancel
    monkeypatch.setattr(cancel, "_POSIX_TTY", False)
    cancel._monitor_active.clear()
    with cancel.cancellable():
        assert not cancel._monitor_active.is_set()
    assert not cancel._monitor_active.is_set()


def test_cbreak_mode_is_noop_on_non_posix(monkeypatch):
    import cancel
    monkeypatch.setattr(cancel, "_POSIX_TTY", False)
    with cancel.cbreak_mode() as cb:
        assert cb.saved is None  # never called termios.tcgetattr


def test_request_cancel_still_works_on_non_posix(monkeypatch):
    """The TUI cancel path is platform-independent and must keep working."""
    import cancel
    monkeypatch.setattr(cancel, "_POSIX_TTY", False)
    cancel.reset()
    assert cancel.is_cancelled() is False
    cancel.request_cancel()
    assert cancel.is_cancelled() is True
    cancel.reset()


# ───────────────────────── bedrock_store.py ─────────────────────────

class _FakeMsvcrt:
    """Minimal stand-in for the Windows ``msvcrt`` locking API."""
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self):
        self.held = False
        self.calls = []

    def locking(self, fd, mode, nbytes):
        self.calls.append(mode)
        if mode == self.LK_NBLCK:
            if self.held:
                raise OSError(errno.EACCES, "lock held")
            self.held = True
        elif mode == self.LK_UNLCK:
            self.held = False


def test_lock_uses_msvcrt_branch_when_fcntl_absent(monkeypatch, tmp_path):
    import bedrock_store
    fake = _FakeMsvcrt()
    monkeypatch.setattr(bedrock_store, "fcntl", None)
    monkeypatch.setattr(bedrock_store, "msvcrt", fake)
    store = tmp_path / "creds.json"
    with bedrock_store.with_locked_store(store) as ctx:
        assert ctx is not None  # lock acquired
        assert fake.held is True
    # Released on exit.
    assert fake.held is False
    assert fake.calls == [fake.LK_NBLCK, fake.LK_UNLCK]


def test_lock_times_out_when_msvcrt_lock_unavailable(monkeypatch, tmp_path):
    """Contended Windows lock must yield None (skip mutation), not corrupt."""
    import bedrock_store
    fake = _FakeMsvcrt()
    fake.held = True  # simulate another process holding it
    monkeypatch.setattr(bedrock_store, "fcntl", None)
    monkeypatch.setattr(bedrock_store, "msvcrt", fake)
    store = tmp_path / "creds.json"
    with bedrock_store.with_locked_store(store, timeout=0.1) as ctx:
        assert ctx is None  # could not acquire → caller skips mutation


def test_lock_best_effort_when_no_primitive(monkeypatch, tmp_path):
    """If neither fcntl nor msvcrt exists, proceed unlocked (single-process)."""
    import bedrock_store
    monkeypatch.setattr(bedrock_store, "fcntl", None)
    monkeypatch.setattr(bedrock_store, "msvcrt", None)
    store = tmp_path / "creds.json"
    with bedrock_store.with_locked_store(store) as ctx:
        assert ctx is not None


# ───────────────────────── exec_command bash resolution ─────────────────────────

@pytest.fixture(autouse=True)
def _reset_bash_cache(monkeypatch):
    from tools import exec_command
    exec_command._BASH_EXE_CACHE = exec_command._UNSET
    # EXEPATH would leak the host's Git-Bash into Windows-path tests; clear it
    # so each test controls detection explicitly.
    monkeypatch.delenv("EXEPATH", raising=False)
    yield
    exec_command._BASH_EXE_CACHE = exec_command._UNSET


def test_bash_exe_env_override(monkeypatch):
    from tools import exec_command
    monkeypatch.setenv("AGENT_BASH_EXE", "/opt/custom/bash")
    assert exec_command._bash_exe() == "/opt/custom/bash"


def test_bash_exe_posix_default(monkeypatch):
    from tools import exec_command
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "posix")
    assert exec_command._bash_exe() == "bash"


def test_bash_exe_windows_uses_which(monkeypatch):
    from tools import exec_command
    import shutil
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda _: r"C:\Git\bin\bash.exe")
    assert exec_command._bash_exe() == r"C:\Git\bin\bash.exe"


def test_bash_exe_windows_falls_back_to_known_path(monkeypatch):
    from tools import exec_command
    import shutil
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    # The code probes <root>\bin\bash.exe via os.path.join — build the expected
    # path the same way so it matches under the POSIX test runner.
    expected = os.path.join(r"C:\Program Files\Git", "bin", "bash.exe")
    monkeypatch.setattr(exec_command.os.path, "exists", lambda p: p == expected)
    assert exec_command._bash_exe() == expected


def test_bash_exe_windows_no_bash_returns_none(monkeypatch):
    """No Git-Bash anywhere and only the WSL stub on PATH → None (never the
    stub, and never bare 'bash' which CreateProcess resolves to the stub)."""
    from tools import exec_command
    import shutil
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "nt")
    monkeypatch.setattr(exec_command.os.path, "exists", lambda p: False)  # no Git-Bash
    monkeypatch.setattr(shutil, "which", lambda _: r"C:\Windows\System32\bash.exe")
    assert exec_command._bash_exe() is None


def test_bash_exe_windows_uses_exepath(monkeypatch):
    """Git-Bash exports EXEPATH=<git root>; it must win even when which()
    only sees the WSL stub (the launched-from-Git-Bash redistribution case)."""
    from tools import exec_command
    import shutil
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "nt")
    root = os.path.join("X:", "Program Files", "Git")
    bash = os.path.join(root, "bin", "bash.exe")
    monkeypatch.setenv("EXEPATH", root)
    monkeypatch.setattr(exec_command.os.path, "exists", lambda p: p == bash)
    monkeypatch.setattr(shutil, "which", lambda _: r"C:\Windows\System32\bash.exe")
    assert exec_command._bash_exe() == bash


def test_bash_exe_windows_prefers_git_over_wsl_stub(monkeypatch):
    """Even when the WSL stub is first on PATH, a present Git-Bash wins."""
    from tools import exec_command
    import shutil
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "nt")
    git_bash = os.path.join(r"C:\Program Files\Git", "bin", "bash.exe")
    monkeypatch.setattr(exec_command.os.path, "exists", lambda p: p == git_bash)
    monkeypatch.setattr(shutil, "which", lambda _: r"C:\Windows\System32\bash.exe")
    assert exec_command._bash_exe() == git_bash


def test_no_bash_error_is_actionable():
    """The no-bash error must name Git for Windows, AGENT_BASH_EXE, and the
    WSL-stub gotcha — not the cryptic WSL message."""
    from tools import exec_command
    msg = exec_command._no_bash_error()
    assert "git-scm.com" in msg
    assert "AGENT_BASH_EXE" in msg
    assert "System32" in msg


def test_exec_command_surfaces_no_bash_error(monkeypatch):
    """End-to-end: when bash is unresolvable, fn() returns the actionable
    error instead of invoking anything (would otherwise hit the WSL stub)."""
    from tools import exec_command
    monkeypatch.setattr(exec_command, "_bash_exe", lambda: None)
    result = exec_command.fn(command="date")
    assert "no usable bash" in result
    assert "git-scm.com" in result


def test_bash_exe_windows_derives_bash_from_git(monkeypatch):
    """The real-world case: git.exe is on PATH (in <root>\\cmd) but bash.exe is
    not, so which('bash') only finds the System32 WSL stub. We must derive
    Git-Bash from the git location instead of returning the broken stub.

    Paths are built with os.path so dirname/join stay consistent under the
    POSIX test runner.
    """
    from tools import exec_command
    import shutil
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "nt")
    root = os.path.join("X:", "Users", "samike", "AppData", "Local", "Programs", "Git")
    git = os.path.join(root, "cmd", "git.exe")
    bash = os.path.join(root, "bin", "bash.exe")
    monkeypatch.setattr(exec_command.os.path, "exists", lambda p: p == bash)
    monkeypatch.setattr(
        shutil, "which",
        lambda name: git if name == "git" else r"C:\Windows\System32\bash.exe",
    )
    assert exec_command._bash_exe() == bash


def test_bash_exe_windows_derives_from_git_mingw_layout(monkeypatch):
    """git.exe at <root>\\mingw64\\bin\\git.exe still resolves bash from root."""
    from tools import exec_command
    import shutil
    monkeypatch.delenv("AGENT_BASH_EXE", raising=False)
    monkeypatch.setattr(exec_command.os, "name", "nt")
    root = os.path.join("X:", "Git")
    git = os.path.join(root, "mingw64", "bin", "git.exe")
    bash = os.path.join(root, "usr", "bin", "bash.exe")
    monkeypatch.setattr(exec_command.os.path, "exists", lambda p: p == bash)
    monkeypatch.setattr(
        shutil, "which",
        lambda name: git if name == "git" else None,
    )
    assert exec_command._bash_exe() == bash
