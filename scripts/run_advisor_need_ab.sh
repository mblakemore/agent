#!/usr/bin/env bash
# Advisor-need A/B: does the deep advisor convert a run the fast model stalls on?
#
# This is the open validation from the advisor-tier arc: earlier A/Bs showed the
# tier ENGAGES but (a) P1 was too hard to convert in the recovery window and
# (b) M2's grind trigger interrupted a slow-but-converging run. The no-progress
# grind gate (cycle.grind_no_progress_turns, on by default) is meant to fix (b)
# by only escalating when the model makes NO file edits for N turns. This run
# measures whether that produces real lift — advisor OFF vs ON, interleaved.
#
# Requires BOTH model servers up:
#   * Qwen 27B driver  @ :8080   (your tmux 'llama')
#   * GLM 744B advisor @ :8000   (your tmux 'llamacpu' / colibri `glm serve`)
#
# Usage:  bash scripts/run_advisor_need_ab.sh
set -u
cd "$(dirname "$0")/.."

echo "── pre-flight: model servers ──"
fail=0
if timeout 8 curl -sf http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
  echo "  ✓ Qwen driver  :8080 UP"
else
  echo "  ✗ Qwen driver  :8080 DOWN — start it (tmux 'llama')"; fail=1
fi
if timeout 8 curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  echo "  ✓ GLM advisor  :8000 UP"
else
  echo "  ✗ GLM advisor  :8000 DOWN — start it (colibri 'glm serve')"; fail=1
fi
[ "$fail" -eq 1 ] && { echo "→ start the server(s) above, then re-run."; exit 2; }

# M2 = medium/solvable (room to convert)  ·  P1 = pathological (hardest stall)
ISSUES="${ISSUES:-M2,P1}"
K="${K:-3}"                      # interleaved reps per arm — single runs are anecdote
TURN_CAP="${TURN_CAP:-50}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-2400}"   # generous: stalls must fire the advisor, not wall-timeout
GRIND_ELAPSED="${GRIND_ELAPSED:-600}"    # slow model: fire grind→advisor after 600s check-red

echo "── advisor A/B — issues=$ISSUES k=$K (no-progress gate ON by default) ──"
python3 scripts/replay_suite.py \
  --live --ab \
  --issues "$ISSUES" \
  --k "$K" \
  --turn-cap "$TURN_CAP" \
  --agent-timeout "$AGENT_TIMEOUT" \
  --grind-elapsed "$GRIND_ELAPSED" \
  --backend-config scripts/backends/qwen-advisor.json
rc=$?

echo
echo "── engagement check (was the advisor actually called? funnel, not just outcome) ──"
echo "  Prometheus (raw counters — 'increase' misses single-sample fires):"
echo "    advisor_escalation{kind=~\"grind.*\"}   — trigger fired"
echo "    agentpy_tool_calls_total{tool=\"consult_advisor\"}  — advisor called"
echo "    advisor_call_total{kind=\"answered\"}    — advisor answered"
echo "  A pass-rate delta is only meaningful if these are >0 on the ON arm."
echo "  Grafana: dashboards/agentpy-fleet.json (advisor panels 14-19)."
exit $rc
