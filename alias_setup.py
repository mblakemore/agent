"""Helpers for the ``/alias`` command.

Detect the working Python launcher and install an ``agent`` shell alias that
maps to ``<python> /path/to/agent.py``. Supports bash/zsh (Linux) and Git-Bash
(Windows) by writing a marked block into the shell rc file; PowerShell and cmd
equivalents are produced for users on those shells. Pure functions here; the
``/alias`` command in commands.py handles user-facing output.
"""

from __future__ import annotations

import os
import shutil
import sys

_MARKER = "# >>> agent alias (added by /alias) >>>"
_MARKER_END = "# <<< agent alias (added by /alias) <<<"


def _in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def detect_python_cmd() -> str:
    """Return a command that launches the *current* Python.

    Inside a virtualenv this is always the venv's own interpreter path: a bare
    ``python3`` would resolve to the system Python once the venv isn't active,
    and that one lacks the dependencies. Otherwise prefers the ``python3``/
    ``python`` name on PATH that matches the running interpreter and isn't the
    Windows Store stub; falls back to the absolute ``sys.executable`` path,
    which always works.
    """
    if _in_venv():
        return sys.executable
    exe_base = os.path.splitext(os.path.basename(sys.executable))[0].lower()
    order = [exe_base] if exe_base in ("python3", "python") else []
    for name in ("python3", "python"):
        if name not in order:
            order.append(name)
    for name in order:
        found = shutil.which(name)
        if found and "windowsapps" not in found.lower():
            return name
    return sys.executable


def agent_script_path() -> str:
    """Absolute path to the running agent.py."""
    main_mod = sys.modules.get("__main__")
    path = getattr(main_mod, "__file__", None) or (sys.argv[0] if sys.argv else "agent.py")
    return os.path.abspath(path)


def _posix_path(p: str) -> str:
    """Forward-slash form — valid for Windows Python and avoids bash backslash
    escaping inside the alias string."""
    return p.replace("\\", "/")


def build_alias_commands(python_cmd: str, agent_path: str) -> dict:
    """Alias/function definitions for each shell, forwarding extra args."""
    bash_path = _posix_path(agent_path)
    return {
        "bash": f"alias agent='{python_cmd} \"{bash_path}\"'",
        "powershell": f'function agent {{ & "{python_cmd}" "{agent_path}" @args }}',
        "cmd": f'doskey agent={python_cmd} "{agent_path}" $*',
    }


def current_shell_kind() -> str:
    """Best-effort shell detection: 'gitbash' | 'zsh' | 'bash' | 'windows'."""
    if os.environ.get("MSYSTEM"):  # Git-Bash / MSYS2 always sets this
        return "gitbash"
    if os.name == "nt":
        return "windows"
    if os.environ.get("SHELL", "").endswith("zsh"):
        return "zsh"
    return "bash"


def rc_file_for(kind: str) -> str | None:
    """The shell rc file to edit for *kind*, or None when there isn't one we
    can safely write (pure Windows cmd/PowerShell)."""
    home = os.path.expanduser("~")
    if kind == "bash" and sys.platform == "darwin":
        # macOS Terminal starts LOGIN shells, which read ~/.bash_profile, not ~/.bashrc.
        return os.path.join(home, ".bash_profile")
    if kind in ("gitbash", "bash"):
        return os.path.join(home, ".bashrc")
    if kind == "zsh":
        return os.path.join(home, ".zshrc")
    return None


def install_alias_block(rc_path: str, alias_line: str) -> str:
    """Idempotently write a marked alias block into *rc_path*.

    Returns 'added' (no prior block), 'updated' (block existed, line changed),
    or 'unchanged' (identical block already present).
    """
    block = f"{_MARKER}\n{alias_line}\n{_MARKER_END}\n"
    existing = ""
    if os.path.exists(rc_path):
        with open(rc_path, encoding="utf-8", errors="replace") as f:
            existing = f.read()

    if _MARKER in existing and _MARKER_END in existing:
        pre = existing.split(_MARKER, 1)[0].rstrip("\n")
        post = existing.split(_MARKER_END, 1)[1].lstrip("\n")
        new = (pre + "\n\n" if pre else "") + block + (post if post else "")
        if new == existing:
            return "unchanged"
        with open(rc_path, "w", encoding="utf-8") as f:
            f.write(new)
        return "updated"

    # A blank line before the block only when there is something above it; on an
    # empty file the leading blank line was stripped by the next run, which then
    # reported "updated" instead of "unchanged".
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(rc_path, "a", encoding="utf-8") as f:
        f.write(sep + ("\n" if existing else "") + block)
    return "added"


def main(argv=None) -> int:
    """``python alias_setup.py [--python CMD] [--rc FILE]`` — used by install.sh.

    Writes the same marked block as ``/alias``, so either one can update it later.
    """
    import argparse
    ap = argparse.ArgumentParser(description="Install the `agent` shell alias.")
    ap.add_argument("--python", help="interpreter command for the alias (default: this one)")
    ap.add_argument("--rc", help="shell rc file to edit (default: from $SHELL)")
    ap.add_argument("--agent", help="path to agent.py (default: next to this file)")
    a = ap.parse_args(argv)
    py = a.python or detect_python_cmd()
    agent = os.path.abspath(a.agent or os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py"))
    line = build_alias_commands(py, agent)["bash"]
    rc = a.rc or rc_file_for(current_shell_kind())
    if not rc:
        print("No shell rc file to edit on this shell. Add this yourself:")
        print("  " + build_alias_commands(py, agent)["powershell"])
        return 1
    status = install_alias_block(rc, line)
    print(f"{rc}: {status}")
    print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
