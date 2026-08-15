# Install

`agent.py` is cross-platform Python. Installing it is separate from getting a model server
running — that is [Setup](setup.md).

```bash
pip install --user -r requirements.txt
```

On Ubuntu 24.04+ (PEP 668 / externally-managed system python), add `--break-system-packages`, or install into a venv:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Windows (Git-Bash) setup

The runtime is cross-platform Python. The `exec_command` tool shells out to `bash` so shell idioms (heredocs, pipes, `&&`, `/tmp/...`) run unchanged — on Windows that bash comes from Git-Bash, **not** `cmd` or PowerShell.

1. **Install [Git for Windows](https://git-scm.com/download/win).** It bundles a standalone `bash.exe` (MSYS2) and `git` — no WSL required.
2. **Install Python 3.10+** for Windows, then `pip install -r requirements.txt`. (Install the GitHub CLI `gh` too if you run the CICD pipeline.)
3. **Make `bash` resolvable.** The runtime locates Git-Bash itself and **deliberately ignores `C:\Windows\System32\bash.exe`** — that's the WSL launcher stub, which fails every command with *"Windows Subsystem for Linux has no installed distributions"* if you don't run WSL. (Bare `bash` would re-resolve straight back to that stub, so it's not used as a fallback.) Resolution order:
   - `AGENT_BASH_EXE` env var pointing at the full path (e.g. `C:\Program Files\Git\bin\bash.exe`, or `%LOCALAPPDATA%\Programs\Git\bin\bash.exe` for a per-user Git install) — **set this if auto-detection fails**, **then**
   - known Git-Bash install locations (`C:\Program Files\Git\bin\bash.exe`, the `(x86)` variant, and the per-user `%LOCALAPPDATA%` path), **then**
   - **derived from `git` on `PATH`** — Git for Windows keeps `bash.exe` in a sibling `bin\` of `git.exe`, so if `git` works, Git-Bash is found even when only Git's `cmd\` (not `bin\`) is on `PATH`, **then**
   - `where bash` on `PATH`, excluding the System32 WSL stub and any `WindowsApps` Store alias.
4. **Run from a Git-Bash shell**, not `cmd`/PowerShell:
   ```bash
   python agent.py "fix the failing test in tests/test_parser.py"
   ```
   For the CICD pipeline: `bash CICD/cicd.sh <repo-url>` from Git-Bash. (A native PowerShell launcher is a separate, not-yet-done port.)
5. **(Optional) shorten the launch command** — run `/alias` inside the agent to install an `agent` shell alias (`<python> /path/to/agent.py` → `agent`). On Git-Bash it writes to `~/.bashrc`; `source ~/.bashrc` (or open a new shell) and then just run `agent`.

**Platform notes:**

- Double-Escape cancellation is a POSIX-tty feature and is a no-op on the Windows console — use `Ctrl+C`, or a TUI host's cancel keybinding.
- The bedrock credential-store lock uses `msvcrt` on Windows (`fcntl` on POSIX); both auto-release on process exit.
- State lives under `%USERPROFILE%\.config\agent\`.
- Native Windows validation is pending a `windows-2022` runner; the suite is currently validated on Linux plus platform-simulation tests (`tests/test_windows_compat.py`).

