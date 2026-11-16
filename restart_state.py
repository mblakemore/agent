"""Restart in place: the state behind /restart and the restart tool (agent.py).

A restart ends the session the way 'exit' does, after the checkpoint is written, and then
main() replaces the process with `python agent.py <same arguments> -c`: the same pid,
terminal and tmux pane, the new code, and the session resumed from the checkpoint. The
request is a flag read at turn boundaries: the loops end on it, run_agent_single returns
"restart" once the turn's tool results are recorded (never mid-tool-call), and main() does
the exec after the normal teardown has restored the terminal. agent.py keeps the gate and
the resume prompt (both read the config) and the checkpoint carries STATE["recent"].
"""
import logging
import os
import sys
import time

STATE = {"requested": False, "by": "", "reason": "", "at": 0.0,
            "resumed": None,     # the checkpoint's restart record, when this run follows one
            "recent": []}        # times of recent restarts (carried in the checkpoint)
LIMIT, WINDOW_S = 3, 600


def too_often(now=None):
    """More than LIMIT restarts within WINDOW_S: a loop, not a fix."""
    now = time.time() if now is None else now
    recent = [t for t in STATE["recent"] if isinstance(t, (int, float)) and now - t < WINDOW_S]
    STATE["recent"] = recent
    return len(recent) >= LIMIT


def request(by="command", reason=""):
    """Ask for the restart; (ok, message). The tool path is refused when restarts are
    coming too fast; the keyboard is never refused."""
    by = str(by or "command")
    reason = " ".join(str(reason or "").split())[:200]
    if STATE["requested"]:
        return True, "a restart is already requested; it happens when this turn ends"
    if by == "tool" and too_often():
        return False, (f"restarted {LIMIT} times in the last {WINDOW_S // 60} minutes; "
                       "not again until that calms down (the operator can /restart at the keyboard)")
    STATE.update(requested=True, by=by, reason=reason, at=time.time())
    STATE["recent"].append(STATE["at"])
    return True, ("restart requested: when this turn ends the process re-executes itself with -c "
                  "and this session continues from its checkpoint (same history, loaded tools and "
                  "skills; monitors armed with persist come back, the rest do not)")


def restart_argv(argv):
    """The arguments to re-execute with: the same ones, `-c` added once."""
    out = [str(a) for a in argv]
    if not any(a in ("-c", "--continue") for a in out[1:]):
        out.insert(1, "-c")
    return out


def exec_restart():
    """Replace this process with `python agent.py ... -c`. Only reached after the session's
    own teardown (terminal restored, monitors stopped, telemetry flushed)."""
    argv = restart_argv(sys.argv)
    sys.stdout.flush()
    sys.stderr.flush()
    logging.shutdown()
    try:
        os.execv(sys.executable, [sys.executable] + argv)
    except OSError as e:
        print(f"restart failed: {e}; start it by hand: {sys.executable} {' '.join(argv)}",
              file=sys.stderr, flush=True)
        sys.exit(3)
