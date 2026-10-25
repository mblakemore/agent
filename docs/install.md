# Install

`agent.py` is cross-platform Python. Installing it is separate from getting a model server
running — that is [Setup](setup.md).

It needs Python 3.10 or newer: Ubuntu 22.04+, Debian 12+, or a Homebrew or python.org Python
on macOS.

## Install script (Linux and macOS)

```bash
git clone https://github.com/mblakemore/agent.git && cd agent
./install.sh
```

By default the script:

1. Picks the newest `python3.x` on your `PATH` that is 3.10 or newer.
2. Creates a venv in `./.venv` and installs `requirements.txt` into it. Nothing is installed into
   the system Python, so it works on PEP 668 "externally managed" systems (Ubuntu 24.04,
   Debian 12, Homebrew Python) without `--break-system-packages`.
3. Checks that `agent.py` starts.
4. Adds an `agent` alias that runs the venv's Python on `agent.py`. The alias goes into
   `~/.bashrc` for bash, `~/.zshrc` for zsh, or `~/.bash_profile` for bash on macOS. You don't
   need to activate the venv.

Then open a new shell, or `source` the file it names, and run `agent --setup`.

Re-running it is safe: the venv is reused, the requirements are re-applied, and the alias block
is replaced, not duplicated. `/alias` inside the agent writes the same block.

| Option | Effect |
| --- | --- |
| `--user` | Install with `pip install --user` into the system Python instead of a venv. On an externally managed Python this adds `--break-system-packages`, which only touches `~/.local` but can still shadow apt-installed packages. |
| `--python PATH` | Use this interpreter. |
| `--no-alias` | Don't touch your shell rc file. |
| `--yes` | Install a missing `python3.X-venv` or `python3-pip` with apt without asking. |

**Ubuntu and Debian:** a minimal server usually has `python3` but not the `venv` module's pip
bootstrap. When that is missing, the script offers to run `sudo apt install python3.12-venv`
(with the version matching your Python). Without a terminal, sudo or `--yes`, it prints the
command and stops.

**macOS:** the `python3` that comes with Xcode's command-line tools is 3.9, which is too old.
Install a newer one with `brew install python@3.12` or from python.org, then run the script.
macOS needs nothing else. The pinned packages ship wheels for Intel and Apple Silicon, and the
`exec_command` tool uses the system `/bin/bash`.

## Manual install

In a venv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python agent.py
```

Or into your user site-packages:

```bash
pip install --user -r requirements.txt
```

On an externally managed Python, the user install needs `--break-system-packages`. The venv
doesn't.

## Dependencies

- Python 3.10+
- `requests`: HTTP to the LLM endpoint
- `transformers`: exact token counts (optional). Off unless `AGENT_TOKENIZER` names the Hugging Face tokenizer matching your model; otherwise a conservative char-based estimate
- `PyMuPDF` (`fitz`): PDF extraction (`tools/read_pdf.py`)
- `markdownify`: HTML → Markdown (`tools/web_fetch.py`)
- `prompt_toolkit`: interactive TUI (optional; falls back to plain `input()` automatically)
- A running OpenAI-compatible LLM server, or a hosted backend such as [AI Commons](aicommons.md)

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

