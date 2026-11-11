"""Arm/stop/list background monitors that inject their output into the loop.

A monitor runs a shell command on an interval; non-empty output pops into the
conversation as a new user turn while you keep working. Fully generic — the
`command` decides what is watched (an inbox poll, a log tail, a file/API
check). Daemon-backed: monitors stop when the cycle ends and the process exits.
"""

import os

import monitor_bus

_MAX_LABEL = 40


def fn(action="arm", label=None, command=None, interval_seconds=15,
       prefix=None, dedup=True, persist=False, path=None, brief=None, bypass=None,
       quiet=None):
    """Manage background monitors.

    Args:
        action: "arm" (start/replace), "stop", or "list". Default "arm".
        label: short name for the monitor (required to arm/stop).
        command: shell command run each interval (required to arm, unless path);
            non-empty stdout is injected into the conversation.
        path: a file to watch instead of a command; what is appended to it is the output.
        interval_seconds: seconds between runs (min 2). Default 15.
        prefix: text prefixed to each injection. Default "[monitor:<label>] ".
        dedup: skip identical consecutive output (default true).
        persist: save the monitor so it is re-armed when agent.py restarts.
        brief: what is worth waking you for; the rest is held for a digest (gate).
        bypass: regex; matching output always wakes you. quiet: regex; always held.
    """
    action = str(action or "arm").strip().lower()

    if action == "list":
        active = monitor_bus.list_active()
        if not active:
            return "No active monitors."
        return "Active monitors:\n" + "\n".join(
            f"  - {m['label']}: `{m['command']}` every {m['interval']:g}s"
            + (" (gated)" if m.get("gated") else "")
            for m in active)

    if action == "stop":
        r = monitor_bus.disarm(label)
        return (f"Monitor '{label}' stopped."
                if r.get("ok") else f"Error: {r.get('error')}")

    if action == "arm":
        if not (isinstance(label, str) and label.strip()):
            return "Error: 'label' is required to arm a monitor."
        if len(label) > _MAX_LABEL:
            return f"Error: label too long (max {_MAX_LABEL} chars)."
        has_cmd = isinstance(command, str) and command.strip()
        has_path = isinstance(path, str) and path.strip()
        if not has_cmd and not has_path:
            return ("Error: 'command' (a shell command to run each interval) or 'path' "
                    "(a file to watch) is required to arm a monitor.")
        if has_path and not os.path.isfile(path.strip()):
            return f"Error: path '{path.strip()}' is not a file."
        gated = bool(monitor_bus.gate_enabled() and (brief or bypass or quiet))
        if (brief or bypass or quiet) and not monitor_bus.gate_enabled():
            return "Error: brief/bypass/quiet need the gate (preferences.gate.enabled)."
        px = prefix if isinstance(prefix, str) else f"[monitor:{label.strip()}] "
        r = monitor_bus.arm(label.strip(), (command or "").strip(),
                            interval=interval_seconds, prefix=px, dedup=bool(dedup),
                            persist=bool(persist), path=(path or "").strip() or None,
                            brief=brief or "", bypass=bypass or "", quiet=quiet or "")
        if not r.get("ok"):
            return f"Error: {r.get('error')}"
        verb = "replaced" if r.get("replaced") else "armed"
        what = (f"watching `{path.strip()}`" if has_path and not has_cmd
                else f"running `{command.strip()}`")
        return (f"Monitor '{label.strip()}' {verb} — {what} every {r['interval']:g}s; "
                f"its output will pop into the conversation as it arrives, at your next "
                f"natural tool-result boundary. "
                + ("It is gated: output is judged against your brief first and the rest "
                   "held for a digest. " if gated else "")
                + ("It is saved and will be re-armed when agent.py restarts. "
                   if r.get("persisted") else "")
                + "It keeps running for as long as this process does: in an "
                  "interactive session that is across many turns, with no need "
                  "to re-arm. In automated (-a) mode the process exits when the "
                  "cycle completes and the monitor goes with it.")

    return f"Error: unknown action '{action}' (use arm|stop|list)."


definition = {
    "type": "function",
    "function": {
        "name": "monitor",
        "description": (
            "Arm a background monitor that runs a shell command on an interval "
            "and pops its output into the conversation as new input while you "
            "keep working. Generic — monitor anything (an inbox poll, a log "
            "tail, a file or API check); the command decides what is watched. "
            "The monitor is a daemon: it runs while the agent process runs "
            "(an interactive session keeps it across turns; an -a cycle ends "
            "it) and never keeps the process alive. Set persist=true to have "
            "it re-armed after agent.py restarts. "
            "Arm it at the start of your work with a label + command to listen "
            "while you proceed; use action='stop'/'list' to manage. Prefer a "
            "command that consumes/advances its own cursor so repeat runs "
            "return nothing until there is something new. path= watches a file "
            "instead (new lines are the output). With the gate enabled, brief= "
            "has the summary model hold what is not worth a wake and digest it later; "
            "wait() blocks until a monitor (or a background job) has something."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["arm", "stop", "list"],
                    "description": "Arm a monitor, stop one, or list active. Default arm.",
                },
                "label": {
                    "type": "string",
                    "description": "Short name for the monitor (required to arm/stop).",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to run each interval (required to arm, unless path). Non-empty stdout is injected.",
                },
                "path": {
                    "type": "string",
                    "description": "A file to watch instead of a command: what is appended to it is the output (a log of a background job).",
                },
                "brief": {
                    "type": "string",
                    "description": "Gate: what is worth waking you for, in your own words; the rest is held for a digest. Needs preferences.gate.enabled.",
                },
                "bypass": {
                    "type": "string",
                    "description": "Gate: regex; matching output always wakes you without a judge call.",
                },
                "quiet": {
                    "type": "string",
                    "description": "Gate: regex; matching output is always held without a judge call.",
                },
                "interval_seconds": {
                    "type": "number",
                    "description": "Seconds between runs (min 2). Default 15.",
                },
                "prefix": {
                    "type": "string",
                    "description": "Text prefixed to each injection. Default '[monitor:<label>] '.",
                },
                "persist": {
                    "type": "boolean",
                    "description": "Save this monitor and re-arm it when agent.py restarts. Default false.",
                },
                "dedup": {
                    "type": "boolean",
                    "description": "Skip identical consecutive output. Default true.",
                },
            },
            "required": ["action"],
        },
    },
}

# Registry hints (tools/__init__.py): catalog phrase, other harnesses' names, search words.
_short = 'run a command on an interval in the background'
_keywords = ['watch', 'poll', 'interval', 'background', 'events']
