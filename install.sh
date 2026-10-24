#!/usr/bin/env bash
# Install agent.py's Python dependencies and an `agent` shell alias.
#
#   ./install.sh               venv in ./.venv (the default; works on PEP 668 systems)
#   ./install.sh --user        pip install --user into the system Python instead, adding
#                              --break-system-packages when that Python is externally managed
#   ./install.sh --python /usr/bin/python3.12     use this interpreter
#   ./install.sh --no-alias    skip the shell alias
#   ./install.sh --yes         don't ask before running apt for missing python3-venv / pip
#
# Safe to re-run: the venv is reused, requirements are re-applied, and the alias block in
# your shell rc file is replaced rather than duplicated (the same block /alias writes).
#
# Written for bash 3.2 too, so it runs on macOS's /bin/bash.

set -euo pipefail

MIN_MAJOR=3
MIN_MINOR=10

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MODE="venv"
PYTHON=""
ALIAS=1
YES=0
VENV="$ROOT/.venv"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --venv)      MODE="venv" ;;
        --user)      MODE="user" ;;
        --python)    [ $# -ge 2 ] || die "--python needs a path"; PYTHON="$2"; shift ;;
        --python=*)  PYTHON="${1#--python=}" ;;
        --no-alias)  ALIAS=0 ;;
        -y|--yes)    YES=1 ;;
        -h|--help)   usage; exit 0 ;;
        *)           usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

OS="$(uname -s)"

# ── pick an interpreter ─────────────────────────────────────────────────────

py_ok() {  # $1 = interpreter; true if it runs and is new enough
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" 2>/dev/null
}

py_version() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?"; }

if [ -n "$PYTHON" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 || die "--python $PYTHON: not found"
    py_ok "$PYTHON" || die "$PYTHON is Python $(py_version "$PYTHON"); agent.py needs $MIN_MAJOR.$MIN_MINOR or newer"
else
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
            PYTHON="$cand"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    found="$(command -v python3 2>/dev/null || true)"
    [ -n "$found" ] && say "Found $found (Python $(py_version "$found")), which is too old."
    case "$OS" in
        Darwin)
            say "agent.py needs Python $MIN_MAJOR.$MIN_MINOR or newer. The python3 that ships with"
            say "Xcode's command-line tools is 3.9. Install a newer one, then re-run this script:"
            say "  brew install python@3.12        (Homebrew)"
            say "  or the installer from https://www.python.org/downloads/macos/" ;;
        *)
            say "agent.py needs Python $MIN_MAJOR.$MIN_MINOR or newer. On Ubuntu 22.04+ / Debian 12+ the"
            say "system python3 is new enough:  sudo apt install python3"
            say "Older releases: use the deadsnakes PPA, pyenv, or uv to get 3.10+, then pass --python." ;;
    esac
    exit 1
fi

PYTHON_PATH="$(command -v "$PYTHON")"
PYV="$(py_version "$PYTHON")"
say "Using $PYTHON_PATH (Python $PYV)"

# ── helpers for Debian/Ubuntu's split python packages ───────────────────────

have_apt() { command -v apt-get >/dev/null 2>&1; }

run_root() {  # run a command as root, via sudo when needed
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

offer_apt() {  # $1.. = packages; returns 0 if installed
    have_apt || return 1
    if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
        return 1
    fi
    if [ "$YES" -ne 1 ]; then
        if [ ! -t 0 ]; then
            return 1
        fi
        printf 'Install %s with apt now? [Y/n] ' "$*"
        read -r reply || reply="n"
        case "$reply" in [nN]*) return 1 ;; esac
    fi
    run_root apt-get update -qq && run_root apt-get install -y "$@"
}

externally_managed() {  # true if this interpreter is PEP 668 "externally managed"
    "$PYTHON" - <<'PY' 2>/dev/null
import os, sys, sysconfig
sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)
PY
}

# ── install ─────────────────────────────────────────────────────────────────

if [ "$MODE" = "venv" ]; then
    step "Creating the virtual environment in $VENV"
    if [ -x "$VENV/bin/python" ] && py_ok "$VENV/bin/python"; then
        say "Reusing the existing venv (Python $(py_version "$VENV/bin/python"))."
    else
        rm -rf "$VENV"
        if ! err="$("$PYTHON" -m venv "$VENV" 2>&1)"; then
            rm -rf "$VENV"
            # Debian/Ubuntu ship venv's pip bootstrap (ensurepip) as a separate package.
            if printf '%s' "$err" | grep -qiE "ensurepip|python3-venv|No module named venv"; then
                pkg="python$PYV-venv"
                say "This Python's venv support is a separate package on Debian/Ubuntu ($pkg)."
                if offer_apt "$pkg"; then
                    "$PYTHON" -m venv "$VENV" || die "venv creation still failed after installing $pkg"
                else
                    say "Install it and re-run this script:"
                    say "  sudo apt install $pkg"
                    say "Or skip the venv:  ./install.sh --user"
                    exit 1
                fi
            else
                printf '%s\n' "$err" >&2
                die "could not create a venv with $PYTHON"
            fi
        fi
    fi
    AGENT_PY="$VENV/bin/python"
    step "Installing requirements into the venv"
    "$AGENT_PY" -m pip install --quiet --timeout 60 --upgrade pip
    "$AGENT_PY" -m pip install --quiet --timeout 60 -r "$ROOT/requirements.txt"
else
    step "Installing requirements into your user site-packages ($PYTHON_PATH)"
    if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
        say "This Python has no pip."
        if offer_apt python3-pip; then :; else
            case "$OS" in
                Darwin) die "run: $PYTHON -m ensurepip --user   then re-run this script" ;;
                *)      die "install pip (sudo apt install python3-pip) and re-run, or use the default venv mode" ;;
            esac
        fi
    fi
    extra=""
    if externally_managed; then
        warn "$PYTHON_PATH is externally managed (PEP 668). Installing with --break-system-packages,"
        warn "into ~/.local only, as you asked with --user. The default venv mode avoids this."
        extra="--break-system-packages"
    fi
    # shellcheck disable=SC2086  # $extra is empty or one flag
    "$PYTHON" -m pip install --quiet --timeout 60 --user $extra -r "$ROOT/requirements.txt"
    AGENT_PY="$PYTHON_PATH"
fi

step "Checking that agent.py starts"
if ! "$AGENT_PY" "$ROOT/agent.py" --help >/dev/null 2>&1; then
    "$AGENT_PY" "$ROOT/agent.py" --help 2>&1 | tail -5 >&2
    die "agent.py did not start with $AGENT_PY"
fi
say "OK"

# ── alias ───────────────────────────────────────────────────────────────────

RC=""
if [ "$ALIAS" -eq 1 ]; then
    step "Adding the 'agent' alias"
    out="$("$AGENT_PY" "$ROOT/alias_setup.py" --python "$AGENT_PY" --agent "$ROOT/agent.py")" || true
    printf '%s\n' "$out"
    RC="$(printf '%s\n' "$out" | sed -n '1s/: [a-z]*$//p')"
fi

step "Done"
if [ -n "$RC" ]; then
    say "Open a new shell, or run:  source $RC"
    say "then:  agent --setup     (point it at a model server; see docs/setup.md)"
else
    say "Run:  $AGENT_PY $ROOT/agent.py --setup"
fi
