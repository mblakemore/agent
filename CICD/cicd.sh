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

mkdir -p "${WORKTREE_ROOT}"

echo "==> CICD loop for ${REPO_NAME}"
echo "    Repo:      ${REPO_URL}"
echo "    Workspace: ${WORKSPACE}"
echo "    Session:   ${SESSION_DIR}"

# ── Clone ─────────────────────────────────────────────────────────────
echo "==> Cloning ${REPO_URL} to ${CLONE_DIR}"
git clone "${REPO_URL}" "${CLONE_DIR}"
cd "${CLONE_DIR}"

# ── Bootstrap dependencies ────────────────────────────────────────────
# Attempt to install project dependencies so the agent has a working
# test environment.  Failures are non-fatal — the agent can still run.
echo "==> Bootstrapping dependencies"
if [[ -f "requirements.txt" || -f "setup.py" || -f "pyproject.toml" || -f "setup.cfg" ]]; then
    python3 -m venv "${SESSION_DIR}/.venv" 2>/dev/null && {
        # shellcheck disable=SC1091
        . "${SESSION_DIR}/.venv/bin/activate"
        pip install --quiet --upgrade pip 2>/dev/null || true
        [[ -f "requirements-dev.txt" ]] && pip install --quiet -r requirements-dev.txt 2>/dev/null || true
        [[ -f "requirements.txt" ]]     && pip install --quiet -r requirements.txt 2>/dev/null || true
        pip install --quiet -e ".[dev]" 2>/dev/null || pip install --quiet -e . 2>/dev/null || true
        pip install --quiet pytest 2>/dev/null || true
        echo "    Python venv: ${SESSION_DIR}/.venv"
    } || echo "    (venv creation failed — skipping Python deps)"
elif [[ -f "package.json" ]]; then
    npm install --quiet 2>/dev/null || echo "    (npm install failed)"
elif [[ -f "go.mod" ]]; then
    go mod download 2>/dev/null || echo "    (go mod download failed)"
elif [[ -f "Cargo.toml" ]]; then
    cargo fetch 2>/dev/null || echo "    (cargo fetch failed)"
else
    echo "    (no recognized dependency file — skipping)"
fi

# ── Build override message ────────────────────────────────────────────
# Tell the agent the real paths so it doesn't use template placeholders.
# If a venv was created, export its PATH so agent subprocesses use it
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
  Example worktree: git worktree add ${WORKTREE_ROOT}/NNN-slug -b cicd/NNN-slug
  Example review:   git worktree add ${WORKTREE_ROOT}/pr-N review/pr-N
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

# ── Run builder ───────────────────────────────────────────────────────
echo "==> Running CICD builder agent"
python3 "${AGENT_PY}" -a --verbose --nudge "${AGENT_MD}

${OVERRIDE}

Follow the instructions and continue!"

# ── Run reviewer ──────────────────────────────────────────────────────
echo "==> Running CICD reviewer agent"
python3 "${AGENT_PY}" -a --verbose --nudge "${REVIEWER_MD}

${OVERRIDE}

Follow the instructions and continue!"

echo "==> Done.  Session: ${SESSION_DIR}"
echo "    CICD state: ${WORKSPACE}/CICD/"
