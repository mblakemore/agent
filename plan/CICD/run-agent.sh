#!/usr/bin/env bash
# Legacy wrapper — delegates to the generalized CICD pipeline.
# Usage: bash plan/CICD/run-agent.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CICD_SH="$(dirname "${SCRIPT_DIR}")/../CICD/cicd.sh"

# Run from the plan/CICD directory so state stays where it was
cd "${SCRIPT_DIR}"
exec "${CICD_SH}" "git@github.com:mblakemore/agent.git"
