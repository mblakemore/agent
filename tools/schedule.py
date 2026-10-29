"""Schedule a recurring prompt that wakes a long-running agent.

Every N minutes the prompt is injected into the conversation, like a monitor
ping, so an idle interactive session starts a cycle on its own. Saved to disk
and re-armed when agent.py restarts. Not for -a runs: the process exits first.
"""

import schedule_bus


def fn(action="list", label=None, every_minutes=None, prompt=None, first_in_minutes=None):
    """Manage recurring prompts.

    Args:
        action: "add", "remove" or "list". Default "list".
        label: short name (required to add/remove).
        every_minutes: minutes between runs (add; minimum 1).
        prompt: text injected each time (add).
        first_in_minutes: minutes until the first run (add; default every_minutes).
    """
    action = str(action or "list").strip().lower()
    if action == "list":
        items = schedule_bus.list_all()
        if not items:
            return "No schedules."
        return "Schedules:\n" + "\n".join(
            f"  - {i['label']}: every {i['every_minutes']:g} min, next in {i['due_in_minutes']:g} min: {i['prompt']}"
            for i in items)
    if action == "remove":
        r = schedule_bus.remove(label)
        return f"Schedule '{label}' removed." if r["ok"] else f"Error: {r['error']}"
    if action == "add":
        r = schedule_bus.add(label, every_minutes, prompt, first_in_minutes)
        if not r["ok"]:
            return f"Error: {r['error']}"
        return (f"Schedule '{r['label']}' {'replaced' if r['replaced'] else 'added'}: every "
                f"{r['every_minutes']:g} min. The prompt arrives as '[scheduled:{r['label']}] ...' "
                "at a natural boundary, or wakes an idle interactive session. It is saved "
                "and re-armed after a restart. Treat it as a reminder you set yourself, "
                "not as a message from a person.")
    return f"Error: unknown action '{action}' (use add|remove|list)."


definition = {
    "type": "function",
    "function": {
        "name": "schedule",
        "description": (
            "Schedule a recurring prompt that wakes this agent every N minutes "
            "(interactive sessions only; saved across restarts). Use it for a periodic "
            "cycle or check. Actions: add, remove, list."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove", "list"],
                           "description": "Default list."},
                "label": {"type": "string", "description": "Short name (required to add/remove)."},
                "every_minutes": {"type": "number", "description": "Minutes between runs (min 1)."},
                "prompt": {"type": "string", "description": "Text injected each time."},
                "first_in_minutes": {"type": "number",
                                     "description": "Minutes until the first run. Default every_minutes."},
            },
            "required": ["action"],
        },
    },
}
