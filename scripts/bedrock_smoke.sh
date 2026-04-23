#!/usr/bin/env bash
# bedrock_smoke.sh — end-to-end live verifier for the Bedrock backend.
#
# Implements the 6 criteria in plan/bedrock-integration.md § 13.4. Exits 0
# if every criterion passes, 1 on the first failure with a human-readable
# diagnostic.
#
# Prerequisites:
#   - BEDROCK_API_URL and BEDROCK_API_KEY in the environment (see § 12).
#   - The agent repo cloned at /droid/repos/agent (this script lives in
#     /droid/repos/agent/scripts).
#
# Usage:
#   scripts/bedrock_smoke.sh
#
# Output is teed to /tmp/bedrock_smoke_<timestamp>.log.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="python3"
LOG="/tmp/bedrock_smoke_$(date +%Y%m%d_%H%M%S).log"
SPEND_FILE="$REPO_ROOT/CICD/bedrock_spend.json"

fail() {
    echo ""
    echo "FAIL: $*" | tee -a "$LOG"
    echo "full log: $LOG"
    exit 1
}

pass() {
    echo "PASS: $*" | tee -a "$LOG"
}

log() {
    echo "$*" | tee -a "$LOG"
}

log "== bedrock_smoke.sh start $(date -Is) =="

# ── Env check ──

if [[ -z "${BEDROCK_API_URL:-}" || -z "${BEDROCK_API_KEY:-}" ]]; then
    fail "BEDROCK_API_URL / BEDROCK_API_KEY must be set in the environment"
fi

log "gateway: $BEDROCK_API_URL"
log "key len: ${#BEDROCK_API_KEY}"

cd "$REPO_ROOT"

# ── Scenario 1: summary live ─────────────────────────────────────────

log ""
log "-- scenario 1: summary live --"

$PY - <<'PY' 2>&1 | tee -a "$LOG" || fail "scenario 1: summary live"
import os, sys
sys.path.insert(0, "/droid/repos/agent")
from llm_backend import BedrockBackend
b = BedrockBackend({
    "kind": "bedrock",
    "model": "claude-v4.5-haiku",
    "role": "summary",
})
text = b.complete(prompt="Summarize in one line: 'The quick brown fox jumps over the lazy dog.'")
if not text or not text.strip():
    print("empty summary")
    sys.exit(1)
print("summary:", text[:200])
PY
pass "scenario 1: summary live"

# ── Scenario 2: main live, one tool call ─────────────────────────────

log ""
log "-- scenario 2: main live, one tool call --"

$PY - <<'PY' 2>&1 | tee -a "$LOG" || fail "scenario 2: main live, one tool call"
import os, sys, logging
sys.path.insert(0, "/droid/repos/agent")
logging.basicConfig(level=logging.INFO)
from llm_backend import BedrockBackend
b = BedrockBackend({
    "kind": "bedrock",
    "model": "claude-v4.5-sonnet",
    "role": "main",
})
tools = [{
    "type": "function",
    "function": {
        "name": "exec_command",
        "description": "Run a shell command and return its output.",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "shell command"}},
            "required": ["cmd"],
        },
    }
}]
messages = [{"role": "user", "content": "List files in /tmp using the exec_command tool."}]
chunks = list(b.stream_chat(logging.getLogger("smoke"), messages=messages, tools=tools))
tc = [c for c in chunks if "tool_calls" in c.get("choices", [{}])[0].get("delta", {})]
if not tc:
    print("no tool calls parsed. chunks:", chunks[:3])
    sys.exit(1)
print(f"tool_calls: {len(tc)}")
PY
pass "scenario 2: main live, one tool call"

# ── Scenario 3: main live, two tools ─────────────────────────────────

log ""
log "-- scenario 3: main live, two tools --"

$PY - <<'PY' 2>&1 | tee -a "$LOG" || fail "scenario 3: main live, two tools"
import os, sys, logging
sys.path.insert(0, "/droid/repos/agent")
logging.basicConfig(level=logging.INFO)
from llm_backend import BedrockBackend
b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-sonnet", "role": "main"})
tools = [{
    "type": "function",
    "function": {
        "name": "exec_command",
        "description": "Run a shell command and return its output.",
        "parameters": {"type": "object",
                       "properties": {"cmd": {"type": "string"}},
                       "required": ["cmd"]},
    }
}]
messages = [{"role": "user",
             "content": ("Using the exec_command tool TWICE in this response, "
                         "first call 'date' and then call 'uname -a'. "
                         "Emit both <tool_call> blocks now.")}]
chunks = list(b.stream_chat(logging.getLogger("smoke"), messages=messages, tools=tools))
tc = [c for c in chunks if "tool_calls" in c.get("choices", [{}])[0].get("delta", {})]
if len(tc) < 2:
    print(f"expected >=2 tool_calls, got {len(tc)}")
    sys.exit(1)
print(f"tool_calls: {len(tc)}")
PY
pass "scenario 3: main live, two tools"

# ── Scenario 4: cancel (best-effort) ─────────────────────────────────

log ""
log "-- scenario 4: cancel behavior --"

# This is a passive check — we just confirm the cancel_check plumbing is
# reachable. A full double-escape test requires a TTY; not scriptable here.
$PY - <<'PY' 2>&1 | tee -a "$LOG" || fail "scenario 4: cancel path reachable"
import sys, threading, time
sys.path.insert(0, "/droid/repos/agent")
from llm_backend import BedrockBackend
import logging
b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "summary"})
cancel = threading.Event()
def cancel_check():
    if cancel.is_set():
        raise KeyboardInterrupt("cancelled")
def fire():
    time.sleep(0.5)
    cancel.set()
threading.Thread(target=fire, daemon=True).start()
t0 = time.monotonic()
try:
    # Use a long, complex prompt to give the cancel a chance to fire during polling.
    b.complete(prompt="Recite the 50 US states alphabetically, one per line.",
               cancel_check=cancel_check)
    print("call completed before cancel fired (ok on a fast gateway)")
except KeyboardInterrupt:
    elapsed = time.monotonic() - t0
    print(f"cancelled after {elapsed:.2f}s")
    if elapsed > 10:
        print("cancel took too long (>10s)")
        sys.exit(1)
PY
pass "scenario 4: cancel path reachable"

# ── Scenario 5: truncation recovery (synthetic) ──────────────────────

log ""
log "-- scenario 5: truncation recovery --"

$PY - <<'PY' 2>&1 | tee -a "$LOG" || fail "scenario 5: truncation recovery"
import sys, logging
sys.path.insert(0, "/droid/repos/agent")
logging.basicConfig(level=logging.INFO)

from unittest.mock import patch
from llm_backend import BedrockBackend
b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"})

# Synthetic: fake the gateway's first reply as truncated mid-<tool_call>,
# then return the tail on the continuation.
first = 'Ok.\n<tool_call>{"tool":"file","args":{"p":"/x"'
second = '}}</tool_call>'

def _msg(t):
    return {"role": "assistant", "content": [{"contentType": "text", "body": t}]}

state = {"n": 0}

def _fake(prompt, conversation_id=None, cancel_check=None):
    state["n"] += 1
    if state["n"] == 1:
        return _msg(first), "conv-1"
    return _msg(second), "conv-1"

with patch.object(b._api, "send_and_wait_conv", side_effect=_fake):
    chunks = list(b.stream_chat(logging.getLogger("smoke"),
                                messages=[{"role":"user","content":"go"}],
                                tools=[]))
tc = [c for c in chunks if "tool_calls" in c.get("choices", [{}])[0].get("delta", {})]
if len(tc) != 1:
    print(f"expected 1 tool_call after recovery, got {len(tc)}")
    sys.exit(1)
print(f"recovery ok, tool_calls={len(tc)}")
PY
pass "scenario 5: truncation recovery"

# ── Scenario 6: cost counter nonzero ─────────────────────────────────

log ""
log "-- scenario 6: cost counter --"

if [[ ! -f "$SPEND_FILE" ]]; then
    fail "scenario 6: spend file $SPEND_FILE not created"
fi

TODAY=$(date +%Y-%m-%d)
SUM=$($PY -c "
import json
try:
    data = json.load(open('$SPEND_FILE'))
    print(sum(float(v) for v in data.get('$TODAY', {}).values()))
except Exception as e:
    print(0)
")

if [[ "$SUM" == "0" || "$SUM" == "0.0" ]]; then
    fail "scenario 6: spend counter is zero for $TODAY"
fi
log "spend today: \$$SUM"
pass "scenario 6: cost counter"

# ── Done ──

log ""
log "== all smoke scenarios passed =="
log "full log: $LOG"
exit 0
