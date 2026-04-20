#!/usr/bin/env bash
# ── CICD Improvement Loop ─────────────────────────────────────────────
# Usage:  cicd <repo-url>
#   e.g.  cicd git@github.com:user/project.git
#
# Run from any workspace directory (e.g. ~/cicdbot1/).  The script
# creates a CICD/ folder in CWD to hold plans, progress, and temp work.
#
# Suggested alias for .bashrc:
#   alias cicd='/droid/repos/agent/CICD/cicd.sh'
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

CICD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # where templates live
AGENT_PY="${AGENT_PY:-/droid/repos/agent/agent.py}"         # override with env var

# ── Args ──────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    echo "Usage: cicd <repo-url>"
    echo "  e.g. cicd git@github.com:user/project.git"
    exit 1
fi

REPO_URL="$1"
REPO_NAME="$(basename "${REPO_URL}" .git)"
WORKSPACE="$(readlink -f .)"  # resolve symlinks for sandbox compatibility

# ── Workspace structure ───────────────────────────────────────────────
#   $WORKSPACE/
#   ├── CICD/
#   │   ├── improvements/    — plan + results files per cycle
#   │   ├── progress.md      — builder progress log
#   │   └── reviews.md       — reviewer log
#   └── temp/
#       └── <stamp>/
#           ├── repo/        — fresh clone
#           └── worktrees/   — git worktrees for branches

mkdir -p "${WORKSPACE}/CICD/improvements"
mkdir -p "${WORKSPACE}/temp"

STAMP="$(date +%Y%m%d_%H%M%S)"
SESSION_DIR="${WORKSPACE}/temp/${STAMP}"
CLONE_DIR="${SESSION_DIR}/repo"
WORKTREE_ROOT="${SESSION_DIR}/worktrees"
export WORKTREE_ROOT
export CICD_MODE=1

mkdir -p "${WORKTREE_ROOT}"

echo "==> CICD loop for ${REPO_NAME}"
echo "    Repo:      ${REPO_URL}"
echo "    Workspace: ${WORKSPACE}"
echo "    Session:   ${SESSION_DIR}"

# ── Clone ─────────────────────────────────────────────────────────────
echo "==> Cloning ${REPO_URL} to ${CLONE_DIR}"
git clone "${REPO_URL}" "${CLONE_DIR}"
cd "${CLONE_DIR}"

# Record system python before any venv activation so agent.py always runs
# with the host interpreter (which has `requests` etc.).
SYSTEM_PYTHON3="$(command -v python3)"

# ── Bootstrap dependencies ────────────────────────────────────────────
# Attempt to install project dependencies so the agent has a working
# test environment.  Failures are non-fatal — the agent can still run.
echo "==> Bootstrapping dependencies"

# Optimization: Use a shared venv cache based on the repo URL to avoid 
# redundant installs across sessions.
VENV_CACHE_DIR="${WORKSPACE}/.venv_cache/$(echo ${REPO_URL} | md5sum | cut -d' ' -f1)"
mkdir -p "$(dirname "${VENV_CACHE_DIR}")"

if [[ -f "requirements.txt" || -f "setup.py" || -f "pyproject.toml" || -f "setup.cfg" ]]; then
    if [[ -d "${VENV_CACHE_DIR}" ]]; then
        echo "    Using cached venv: ${VENV_CACHE_DIR}"
        cp -rp "${VENV_CACHE_DIR}" "${SESSION_DIR}/.venv"
    else
        python3 -m venv "${SESSION_DIR}/.venv" 2>/dev/null && {
            # shellcheck disable=SC1091
            . "${SESSION_DIR}/.venv/bin/activate"
            pip install --quiet --upgrade pip 2>/dev/null || true
            [[ -f "requirements-dev.txt" ]] && pip install --quiet -r requirements-dev.txt 2>/dev/null || true
            [[ -f "requirements.txt" ]]     && pip install --quiet -r requirements.txt 2>/dev/null || true
            pip install --quiet -e ".[dev]" 2>/dev/null || pip install --quiet -e . 2>/dev/null || true
            pip install --quiet pytest 2>/dev/null || true
            echo "    Python venv: ${SESSION_DIR}/.venv"
            # Cache the result
            cp -rp "${SESSION_DIR}/.venv" "${VENV_CACHE_DIR}"
        } || echo "    (venv creation failed — skipping Python deps)"
    fi
elif [[ -f "package.json" ]]; then
    npm install --quiet 2>/dev/null || echo "    (npm install failed)"
elif [[ -f "go.mod" ]]; then
    go mod download 2>/dev/null || echo "    (go mod download failed)"
elif [[ -f "Cargo.toml" ]]; then
    cargo fetch 2>/dev/null || echo "    (cargo fetch failed)"
elif ls *.py tests/*.py 2>/dev/null | head -1 | grep -q .; then
    # Python repo without a manifest — create a venv with pytest + common deps
    if [[ -d "${VENV_CACHE_DIR}" ]]; then
        echo "    Using cached venv: ${VENV_CACHE_DIR}"
        cp -rp "${VENV_CACHE_DIR}" "${SESSION_DIR}/.venv"
    else
        python3 -m venv "${SESSION_DIR}/.venv" 2>/dev/null && {
            # shellcheck disable=SC1091
            . "${SESSION_DIR}/.venv/bin/activate"
            pip install --quiet --upgrade pip 2>/dev/null || true
            pip install --quiet pytest requests markdownify 2>/dev/null || true
            # Install any importable packages found in the repo's imports
            grep -rh "^import \|^from " *.py tools/*.py 2>/dev/null \
                | sed 's/^import //;s/^from //;s/ .*//' | sort -u \
                | while read -r mod; do
                    python3 -c "import $mod" 2>/dev/null || pip install --quiet "$mod" 2>/dev/null || true
                done
            echo "    Python venv (auto-deps): ${SESSION_DIR}/.venv"
            # Cache the result
            cp -rp "${SESSION_DIR}/.venv" "${VENV_CACHE_DIR}"
        } || echo "    (venv creation failed — skipping Python deps)"
    fi
else
    echo "    (no recognized dependency file — skipping)"
fi

# ── Build override message ────────────────────────────────────────────
# Tell the agent the real paths so it doesn't use template placeholders.
VENV_NOTE=""
if [[ -d "${SESSION_DIR}/.venv" ]]; then
    export PATH="${SESSION_DIR}/.venv/bin:${PATH}"
    VENV_NOTE="  Python venv: ${SESSION_DIR}/.venv (activated — pytest and deps available)"
fi

OVERRIDE="$(cat <<EOVERRIDE
NOTE — Session paths (use these instead of any template placeholders):
  Target repo:   ${CLONE_DIR}
  Worktree root: ${WORKTREE_ROOT}
  CICD state:    ${WORKSPACE}/CICD
  Instructions: Create worktrees in the Worktree root directory using standard git commands.
  Sandbox boundary: ${SESSION_DIR} — all paths must be under here or under ${WORKSPACE}/CICD.
  Repo name: ${REPO_NAME}
  Repo URL:  ${REPO_URL}
${VENV_NOTE}
EOVERRIDE
)"

# ── Load and inject templates ─────────────────────────────────────────
# Strip @ before path-like refs so agent.py's _expand_file_refs
# doesn't try to re-expand documentation references in the body text.
AGENT_MD="$(sed 's/@\([a-zA-Z./]\)/\1/g' "${CICD_HOME}/agent.md")"
REVIEWER_MD="$(sed 's/@\([a-zA-Z./]\)/\1/g' "${CICD_HOME}/reviewer.md")"

# ── Pre-seed builder task list ────────────────────────────────────────
# Saves ~5 turns the builder would otherwise spend adding standard tasks
# one per turn (LLM output pattern). Template instructs the builder to
# `list` first and not re-add if present.
mkdir -p "${CLONE_DIR}/.agent/state"
cat > "${CLONE_DIR}/.agent/state/tasks.json" <<'EOT'
[
  {"id": 1, "description": "PERCEIVE: gather repo state, issues, test status", "status": "open", "created": "pre-seeded"},
  {"id": 2, "description": "DECIDE: pick issue, state metric and done-when", "status": "open", "created": "pre-seeded"},
  {"id": 3, "description": "IMPLEMENT: code the fix in worktree", "status": "open", "created": "pre-seeded"},
  {"id": 4, "description": "VERIFY: tests green + metric improved", "status": "open", "created": "pre-seeded"},
  {"id": 5, "description": "TRACK: results file, progress row, PR, issue comment", "status": "open", "created": "pre-seeded"},
  {"id": 6, "description": "CLEANUP: remove worktree", "status": "open", "created": "pre-seeded"}
]
EOT

# ── Run builder ───────────────────────────────────────────────────────
echo "==> Running CICD builder agent"
"${SYSTEM_PYTHON3}" "${AGENT_PY}" -a --verbose --nudge "${AGENT_MD}

${OVERRIDE}

Follow the instructions and continue!"

# ── Run reviewer ──────────────────────────────────────────────────────
echo "==> Running CICD reviewer agent"
"${SYSTEM_PYTHON3}" "${AGENT_PY}" -a --verbose --nudge "${REVIEWER_MD}

${OVERRIDE}

Follow the instructions and continue!"

echo "==> Done.  Session: ${SESSION_DIR}"
echo "    CICD state: ${WORKSPACE}/CICD/"
