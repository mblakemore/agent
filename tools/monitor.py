"""Arm/stop/list background monitors that inject their output into the loop.

A monitor runs a shell command on an interval; non-empty output pops into the
conversation as a new user turn while you keep working. Fully generic — the
`command` decides what is watched (an inbox poll, a log tail, a file/API
check). Daemon-backed: monitors stop when the cycle ends and the process exits.
"""

import monitor_bus

_MAX_LABEL = 40


def fn(action="arm", label=None, command=None, interval_seconds=15,
       prefix=None, dedup=True):
    """Manage background monitors.

    Args:
        action: "arm" (start/replace), "stop", or "list". Default "arm".
        label: short name for the monitor (required to arm/stop).
        command: shell command run each interval (required to arm); non-empty
            stdout is injected into the conversation.
        interval_seconds: seconds between runs (min 2). Default 15.
        prefix: text prefixed to each injection. Default "[monitor:<label>] ".
        dedup: skip identical consecutive output (default true).
    """
    action = str(action or "arm").strip().lower()

    if action == "list":
        active = monitor_bus.list_active()
        if not active:
            return "No active monitors."
        return "Active monitors:\n" + "\n".join(
            f"  - {m['label']}: `{m['command']}` every {m['interval']:g}s"
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
        if not (isinstance(command, str) and command.strip()):
            return ("Error: 'command' is required to arm a monitor "
                    "(a shell command to run each interval).")
        px = prefix if isinstance(prefix, str) else f"[monitor:{label.strip()}] "
        r = monitor_bus.arm(label.strip(), command.strip(),
                            interval=interval_seconds, prefix=px, dedup=bool(dedup))
        if not r.get("ok"):
            return f"Error: {r.get('error')}"
        verb = "replaced" if r.get("replaced") else "armed"
        return (f"Monitor '{label.strip()}' {verb} — running "
                f"`{command.strip()}` every {r['interval']:g}s; its output will "
                f"pop into the conversation as it arrives. It stops when the "
                f"cycle ends.")

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
            "The monitor is a daemon: it stops automatically when the cycle "
            "ends and the process exits (it never keeps the process alive). "
            "Arm it at the start of your work with a label + command to listen "
            "while you proceed; use action='stop'/'list' to manage. Prefer a "
            "command that consumes/advances its own cursor so repeat runs "
            "return nothing until there is something new."),
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
                    "description": "Shell command to run each interval (required to arm). Non-empty stdout is injected.",
                },
                "interval_seconds": {
                    "type": "number",
                    "description": "Seconds between runs (min 2). Default 15.",
                },
                "prefix": {
                    "type": "string",
                    "description": "Text prefixed to each injection. Default '[monitor:<label>] '.",
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
