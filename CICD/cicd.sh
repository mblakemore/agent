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

# ── Build override message ────────────────────────────────────────────
# Tell the agent the real paths so it doesn't use template placeholders.
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
