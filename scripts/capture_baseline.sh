#!/usr/bin/env bash
# Capture baseline behavior of the agent before the UI upgrade.
# See plan/ui-upgrade-from-llmbox-cli.md §11.1.
#
# Runs a fixed set of scenarios against the pinned llama-server endpoints with
# deterministic generation params (temperature 0) and normalizes the output so
# future phases can diff against a committed baseline.
#
# Requirements:
#   - Main   llama-server at http://127.0.0.1:8080  (run_server_hf.sh)
#   - Summary llama-server at http://127.0.0.1:8082 (run_server_hf_e4b_cpu.sh)
#
# Output: baseline/<scenario>.{stdout,history}.log

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
BASELINE="$REPO/baseline"
mkdir -p "$BASELINE"

# ── 0. Prerequisite: both endpoints up ──────────────────────────────────
check_endpoint() {
    local name="$1" url="$2" launcher="$3"
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 3 "$url" || echo 000)
    if [ "$code" != "200" ]; then
        echo "ERROR: $name llama-server not reachable ($url returned $code)" >&2
        echo "       start it with: $launcher" >&2
        exit 1
    fi
    echo "  $name OK ($url)"
}
echo "Checking endpoints..."
check_endpoint "main"    "http://127.0.0.1:8080/health" "/mnt/droid/repos/llama.cpp/run_server_hf.sh"
check_endpoint "summary" "http://127.0.0.1:8082/health" "/mnt/droid/repos/llama.cpp/run_server_hf_e4b_cpu.sh"

# ── 1. Install deterministic config ─────────────────────────────────────
CONFIG_BAK=""
if [ -f config.json ]; then
    CONFIG_BAK="config.json.baseline_bak.$$"
    cp config.json "$CONFIG_BAK"
fi
cat > config.json <<'JSON'
{
  "generation": {
    "temperature": 0,
    "top_p": 1.0,
    "top_k": 1,
    "presence_penalty": 0
  },
  "cycle": {
    "max_turns": 20
  }
}
JSON

restore_config() {
    if [ -n "$CONFIG_BAK" ] && [ -f "$CONFIG_BAK" ]; then
        mv "$CONFIG_BAK" config.json
    else
        rm -f config.json
    fi
}
trap restore_config EXIT

# ── 2. Helpers ──────────────────────────────────────────────────────────
fresh_state() {
    # Wipe anything the agent writes between scenarios.
    # Works for both the current (pre-F0) layout and the .agent/ layout.
    rm -rf state agent_history .agent
}

# Normalizer: strip timestamps, PIDs, cycle ids, home paths, ANSI escapes,
# session ids — anything that legitimately varies between runs but would noise
# a regression diff.
normalize() {
    local home_escaped="${HOME//\//\\/}"
    sed -E \
        -e 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}[.,][0-9]+/TIMESTAMP/g' \
        -e 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}/TIMESTAMP/g' \
        -e 's/[0-9]{8}_[0-9]{6}/DATESTAMP/g' \
        -e 's/[0-9]{2}:[0-9]{2}:[0-9]{2}/HH:MM:SS/g' \
        -e "s/${home_escaped}/\$HOME/g" \
        -e 's/pid[ =:][0-9]+/pid=PID/gI' \
        -e 's/cycle[-_][0-9]+/cycle-N/gI' \
        -e 's/session[-_][a-f0-9-]{6,}/session-SID/gI' \
        -e 's/\bturn [0-9]+/turn N/gI' \
        -e 's/\belapsed[: =][0-9]+\.[0-9]+s?/elapsed=N.Ns/gI' \
        -e 's/[0-9]+\.[0-9]+s\b/N.Ns/g' \
        -e $'s/\x1b\\[[0-9;]*[A-Za-z]//g' \
        -e $'s/\x1b\\][0-9]*;[^\x07]*\x07//g' \
        -e 's/[0-9]+\.[0-9]+ ?t\/s/N.N t\/s/g' \
        -e 's/[0-9]+ ?tokens/N tokens/g'
}

run_scenario() {
    local name="$1"; shift
    echo "  → $name"
    fresh_state
    set +e
    # Sandbox PATH: prepend the git-shim dir so the agent-under-capture
    # cannot commit/push/reset/… against the real repo. See
    # scripts/capture_sandbox/git for the blocklist. This is defense against
    # the nudge-scenario rogue-commit trap (plan § 16 Phase 1 friction).
    env PATH="$REPO/scripts/capture_sandbox:$PATH" \
        python3 agent.py "$@" > "$BASELINE/$name.stdout.raw" 2>&1
    local rc=$?
    set -e
    # Collect whichever history path exists.
    : > "$BASELINE/$name.history.raw"
    for d in agent_history .agent/history; do
        if compgen -G "$d/*.log" > /dev/null 2>&1; then
            cat "$d"/*.log >> "$BASELINE/$name.history.raw"
        fi
    done
    normalize < "$BASELINE/$name.stdout.raw"  > "$BASELINE/$name.stdout.log"
    normalize < "$BASELINE/$name.history.raw" > "$BASELINE/$name.history.log"
    rm -f "$BASELINE/$name.stdout.raw" "$BASELINE/$name.history.raw"
    echo "     exit=$rc  stdout=$(wc -l < "$BASELINE/$name.stdout.log") lines  history=$(wc -l < "$BASELINE/$name.history.log") lines"
}

# ── 3. Scenarios ────────────────────────────────────────────────────────
echo "Running scenarios..."

run_scenario simple     -a "read README.md and summarize in 2 sentences"
run_scenario multi_tool -a "create a file named scratch.txt containing 'hi', then delete it"
run_scenario tool_error -a "read the file /nonexistent/definitely-not-here.txt"
# nudge: must be a prompt that has no natural tool affordance — abstract fact
# recall, not a task. Earlier "think out loud about sorting algorithms without
# using any tools" backfired; the model ignored the disclaimer and opened an
# exec_command / file session to build a benchmark. Straight definitional Q&A
# is harder to tool-ify.
run_scenario nudge      -a --nudge "explain the difference between TCP and UDP in 3 sentences"

# Scenario 5 (resume via -c) is deferred: -a deletes the checkpoint on clean
# exit, so capturing a resume path requires either interrupting a run or
# hand-crafting a checkpoint. Added as a manual smoke test in plan §11.4 for
# now; add here once F0 lands and we can snapshot .agent/state mid-run.

# ── 4. Summary ──────────────────────────────────────────────────────────
fresh_state
echo
echo "Baseline captures:"
ls -la "$BASELINE"
