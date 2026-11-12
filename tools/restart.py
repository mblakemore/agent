"""restart: re-execute the agent process with -c, so the same session continues on the
code now on disk (agent.py, a tool, a skill file, the config).

What `/restart` does from the keyboard: when the turn that called this tool ends, the
checkpoint is written, the session is torn down like an 'exit', and main() replaces the
process with `python agent.py <the same arguments> -c`: same pid, terminal and tmux pane;
the history, loaded tools and skills come back from the checkpoint, monitors armed with
persist come back from .agent/monitors.json, other monitors and background jobs do not.

Opt-in: `preferences.restart_tool` true; off, the tool does not exist to the model
(agent.py gates it). More than three restarts in ten minutes are refused.
"""

import tools as _registry

_tier = "hidden"       # deferred (named in the catalog) once preferences.restart_tool is true
_short = "restart the agent process, same session"
_keywords = ["restart", "relaunch", "reload", "re-exec", "new code", "same session"]


def fn(reason: str = "") -> str:
    req = _registry.RUNTIME.get("request_restart")
    if not callable(req):
        return "Error: this harness exposes no restart control (tools.RUNTIME is empty)"
    try:
        ok, msg = req(by="tool", reason=reason)
    except Exception as e:  # noqa: BLE001 — report, never crash the turn
        return f"Error: restart request failed: {e}"
    return msg if ok else f"Error: {msg}"


definition = {
    "type": "function",
    "function": {
        "name": "restart",
        "description": (
            "Restart the agent process when this turn ends: it re-executes itself with -c "
            "and this same session continues from its checkpoint on the code now on disk. "
            "Use it after the harness, a tool, a skill or the config changed on disk. Say "
            "what you are about to do first; nothing more happens in this turn after the "
            "call. Monitors without persist and background jobs are lost."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why (one line); shown at the keyboard and in the note after the restart.",
                },
            },
            "required": [],
        },
    },
}
