#!/usr/bin/env bash
# ── CICD Improvement Loop — Claude Code edition ───────────────────────
# Usage:  cicd_claude <repo-url>
#   e.g.  cicd_claude git@github.com:user/project.git
#
# Drop-in replacement for cicd.sh that uses `claude -p` (Claude Code
# headless) instead of agent.py.  Requires ANTHROPIC_API_KEY.
#
# Env overrides:
#   BOT_ID=2              multi-bot namespace (default: 1)
#   CICD_PHASE=builder    builder | reviewer | both (default: both)
#   CICD_MODEL=sonnet     claude model alias (default: opus)
#   CICD_BUDGET=10.00     max USD per phase via --max-budget-usd (default: 10.00)
#   CICD_MEM_MAX=20G      systemd memory cap (default: 12G)
#   CICD_NO_SCOPE=1       skip systemd-run wrapping
#
# Suggested alias for .bashrc:
#   alias cicd_claude='/droid/repos/agent/CICD/cicd_claude.sh'
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

CICD_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # where templates live

# ── Args ──────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    echo "Usage: cicd_claude <repo-url>"
    echo "  e.g. cicd_claude git@github.com:user/project.git"
    exit 1
fi

REPO_URL="$1"
REPO_NAME="$(basename "${REPO_URL}" .git)"
WORKSPACE="$(readlink -f .)"
export BOT_ID="${BOT_ID:-1}"
CICD_PHASE="${CICD_PHASE:-both}"
CICD_MODEL="${CICD_MODEL:-opus}"
CICD_BUDGET="${CICD_BUDGET:-10.00}"

# ── Workspace structure ───────────────────────────────────────────────
mkdir -p "${WORKSPACE}/CICD/improvements"
mkdir -p "${WORKSPACE}/temp"

STAMP="$(date +%Y%m%d_%H%M%S)"
SESSION_DIR="${WORKSPACE}/temp/${STAMP}"
CLONE_DIR="${SESSION_DIR}/repo"
WORKTREE_ROOT="${SESSION_DIR}/worktrees"
export WORKTREE_ROOT
export CICD_MODE=1

mkdir -p "${WORKTREE_ROOT}"

echo "==> CICD loop for ${REPO_NAME} (claude -p / ${CICD_MODEL})"
echo "    Repo:      ${REPO_URL}"
echo "    Workspace: ${WORKSPACE}"
echo "    Session:   ${SESSION_DIR}"
echo "    Phase:     ${CICD_PHASE}"

# ── Clone ─────────────────────────────────────────────────────────────
echo "==> Cloning ${REPO_URL} to ${CLONE_DIR}"
git clone "${REPO_URL}" "${CLONE_DIR}"
cd "${CLONE_DIR}"

SYSTEM_PYTHON3="$(command -v python3)"

# ── Bootstrap dependencies ────────────────────────────────────────────
echo "==> Bootstrapping dependencies"

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
    if [[ -d "${VENV_CACHE_DIR}" ]]; then
        echo "    Using cached venv: ${VENV_CACHE_DIR}"
        cp -rp "${VENV_CACHE_DIR}" "${SESSION_DIR}/.venv"
    else
        python3 -m venv "${SESSION_DIR}/.venv" 2>/dev/null && {
            # shellcheck disable=SC1091
            . "${SESSION_DIR}/.venv/bin/activate"
            pip install --quiet --upgrade pip 2>/dev/null || true
            pip install --quiet pytest requests markdownify 2>/dev/null || true
            grep -rh "^import \|^from " *.py tools/*.py 2>/dev/null \
                | sed 's/^import //;s/^from //;s/ .*//' | sort -u \
                | while read -r mod; do
                    "${SYSTEM_PYTHON3}" -c "import $mod" 2>/dev/null || pip install --quiet "$mod" 2>/dev/null || true
                done
            echo "    Python venv (auto-deps): ${SESSION_DIR}/.venv"
            cp -rp "${SESSION_DIR}/.venv" "${VENV_CACHE_DIR}"
        } || echo "    (venv creation failed — skipping Python deps)"
    fi
else
    echo "    (no recognized dependency file — skipping)"
fi

# ── Build override message ────────────────────────────────────────────
VENV_NOTE=""
if [[ -d "${SESSION_DIR}/.venv" ]]; then
    export PATH="${SESSION_DIR}/.venv/bin:${PATH}"
    VENV_NOTE="  Python venv: ${SESSION_DIR}/.venv (activated — pytest and deps available)"
fi

# Tool name mapping — agent.md was written for agent.py tool names;
# Claude Code uses different names for the same operations.
TOOL_MAP="
TOOL NAME MAPPING (this session uses Claude Code built-in tools):
  agent.py name     → Claude Code tool
  exec_command      → Bash
  file(action=read) → Read
  file(action=write)→ Write
  file(action=insert/delete/append) → Edit
  search_files      → Grep
  subagent          → Agent
  web_fetch         → WebFetch
  find_symbol       → Grep (use -n for line numbers)
Use these Claude Code tool names throughout this session."

OVERRIDE="$(cat <<EOVERRIDE
NOTE — Session paths (use these instead of any template placeholders):
  Target repo:   ${CLONE_DIR}
  Worktree root: ${WORKTREE_ROOT}
  CICD state:    ${WORKSPACE}/CICD
  Instructions: Create worktrees in the Worktree root directory using standard git commands.
  Sandbox boundary: ${SESSION_DIR} — all paths must be under here or under ${WORKSPACE}/CICD.
  Repo name: ${REPO_NAME}
  Repo URL:  ${REPO_URL}
  Bot ID: ${BOT_ID} — use this value for namespaced state files and labels (see templates)
${VENV_NOTE}
${TOOL_MAP}
EOVERRIDE
)"

# ── Load and inject templates ─────────────────────────────────────────
AGENT_MD="$(sed 's/@\([a-zA-Z./]\)/\1/g' "${CICD_HOME}/agent.md" | envsubst '$BOT_ID')"
REVIEWER_MD="$(sed 's/@\([a-zA-Z./]\)/\1/g' "${CICD_HOME}/reviewer.md" | envsubst '$BOT_ID')"

# ── Pre-create cicd-cycle and bot-claim labels ────────────────────────
echo "==> Pre-creating cicd-cycle and bot-claim labels"
_NEXT_ISSUE="$(gh issue list --state all --repo "${REPO_URL}" --limit 1 --json number --jq '.[0].number + 1' 2>/dev/null || echo "1")"
for _i in 0 1 2; do
    _N=$((_NEXT_ISSUE + _i))
    gh label create "cicd-cycle-${_N}" --color "0e8a16" \
        --description "CICD cycle ${_N}" \
        --repo "${REPO_URL}" 2>/dev/null || true
done
gh label create "in-progress-bot-${BOT_ID}" --color "0e8a16" \
    --description "Claimed by CICD bot ${BOT_ID}" \
    --repo "${REPO_URL}" 2>/dev/null || true
unset _NEXT_ISSUE _i _N

# ── Pre-seed builder task list ────────────────────────────────────────
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

# ── Memory isolation (cgroup scope) ──────────────────────────────────
CICD_MEM_MAX="${CICD_MEM_MAX:-12G}"
SCOPE_WRAP=()
if [ -z "${CICD_NO_SCOPE:-}" ] && command -v systemd-run &>/dev/null; then
    SCOPE_WRAP=(systemd-run --user --scope --quiet --same-dir
                --property=MemoryMax="${CICD_MEM_MAX}"
                --property=MemorySwapMax=0)
    echo "==> Memory scope: ${CICD_MEM_MAX} max (systemd-run --user --scope)"
fi

# ── Run builder ───────────────────────────────────────────────────────
if [[ "${CICD_PHASE}" == "builder" || "${CICD_PHASE}" == "both" ]]; then
    echo "==> Running CICD builder (claude -p --model ${CICD_MODEL})"
    "${SCOPE_WRAP[@]}" claude -p "${AGENT_MD}

${OVERRIDE}

Follow the instructions and continue!" \
        --model "${CICD_MODEL}" \
        --dangerously-skip-permissions \
        --max-budget-usd "${CICD_BUDGET}" \
        --output-format text \
        --verbose
fi

# ── Run reviewer ──────────────────────────────────────────────────────
if [[ "${CICD_PHASE}" == "reviewer" || "${CICD_PHASE}" == "both" ]]; then
    echo "==> Running CICD reviewer (claude -p --model ${CICD_MODEL})"
    "${SCOPE_WRAP[@]}" claude -p "${REVIEWER_MD}

${OVERRIDE}

Follow the instructions and continue!" \
        --model "${CICD_MODEL}" \
        --dangerously-skip-permissions \
        --max-budget-usd "${CICD_BUDGET}" \
        --output-format text \
        --verbose
fi

echo "==> Done.  Session: ${SESSION_DIR}"
echo "    CICD state: ${WORKSPACE}/CICD/"
