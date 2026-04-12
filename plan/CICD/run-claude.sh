#!/usr/bin/env bash
# CICD loop runner — Claude Code CLI
# Creates a temp clone under /droid/temp/, runs the builder agent,
# then the reviewer agent, both operating against the clone.
set -euo pipefail

REPO_URL="git@github.com:mblakemore/agent.git"
TEMP_ROOT="/droid/temp"
STAMP="$(date +%Y%m%d_%H%M%S)"
WORKDIR="${TEMP_ROOT}/cicd-${STAMP}/agent"

mkdir -p "${TEMP_ROOT}"
echo "==> Cloning repo to ${WORKDIR}"
git clone "${REPO_URL}" "${WORKDIR}"
cd "${WORKDIR}"

OVERRIDE="NOTE: The target repo for this session is ${WORKDIR} — use it in place of /mnt/droid/repos/agent everywhere (worktree parents, gh commands, test runs, etc.)."

echo "==> Running CICD agent"
claude --dangerously-skip-permissions -p --verbose --output-format text \
  "@./plan/CICD/agent.md ${OVERRIDE} Follow the instructions and continue!"

echo "==> Running CICD reviewer"
claude --dangerously-skip-permissions -p --verbose --output-format text \
  "@./plan/CICD/reviewer.md ${OVERRIDE} Follow the instructions and continue!"

echo "==> Done.  Workdir: ${WORKDIR}"
