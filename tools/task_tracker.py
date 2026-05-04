"""Task tracker tool — persistent task management via .agent/state/tasks.json."""

import json
from datetime import datetime
from pathlib import Path


_TASKS_FILE = ".agent/state/tasks.json"


def _load_tasks():
    p = Path(_TASKS_FILE)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding='utf-8', errors='replace'))
    except (json.JSONDecodeError, IOError):
        return []

def get_tasks():
    """Return the current list of tasks."""
    return _load_tasks()



def _save_tasks(tasks):
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tasks, indent=2) + "\n", encoding='utf-8')


def _next_id(tasks):
    return max((t.get("id", 0) for t in tasks), default=0) + 1


def fn(action: str, description: str = "", task_id: int = 0, status: str = "") -> str:
    """Manage persistent tasks.

    Args:
        action: One of "add", "done", "update", "drop", "list".
        description: Task description (for add) or note (for update). Optional — omit for list/done/drop.
        task_id: Task ID (for done, update, drop).
        status: New status string (for update). Common: "in_progress", "blocked", "deferred".
    """
    # Ensure description is always a string even if the model omits the field
    description = description or ""

    # Validate task_id type — must be an integer (or the default 0).
    # Non-integer values (e.g. strings passed by a model) would cause
    # TypeError at the `task_id <= 0` comparisons below.
    if not isinstance(task_id, int):
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return f"Error: task_id must be an integer, got {type(task_id).__name__!r}: {task_id!r}"

    tasks = _load_tasks()

    # Treat update with completed/done status as the "done" action
    if action == "update" and status in ("completed", "done"):
        action = "done"

    # Auto-resolve task_id: if missing, try to find a unique open task
    if action in ("done", "update", "drop") and task_id <= 0:
        open_tasks = [t for t in tasks if t["status"] not in ("done", "completed")]
        if len(open_tasks) == 1:
            task_id = open_tasks[0]["id"]
        elif description:
            # Try fuzzy match by description substring
            desc_lower = description.lower()
            matches = [t for t in open_tasks if desc_lower in t.get("description", "").lower()
                       or t.get("description", "").lower() in desc_lower]
            if len(matches) == 1:
                task_id = matches[0]["id"]

    if action == "add":
        if not description:
            if status and task_id > 0:
                return (f"Error: 'add' requires description. To change status of an "
                        f"existing task, use action='update' with task_id={task_id}, status='{status}'.")
            if status:
                return (f"Error: 'add' requires description. To set status on an "
                        f"existing task, use action='update' with task_id=<N>, status='{status}'.")
            return "Error: description required for 'add'"
        existing = next((t for t in tasks if t["status"] not in ("done", "completed")
                         and t.get("description", "").strip() == description.strip()), None)
        if existing:
            return f"Task #{existing['id']} already exists (open): {description}"
        task = {
            "id": _next_id(tasks),
            "description": description,
            "status": "open",
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        tasks.append(task)
        _save_tasks(tasks)
        return f"Added task #{task['id']}: {description}"

    elif action == "done":
        if task_id <= 0:
            available = [f"#{t['id']} ({t['status']}): {t.get('description', '')}" for t in tasks if t["status"] != "done"]
            return f"Error: task_id required for 'done'. Example: task_tracker(action=\"done\", task_id=1)\nOpen tasks:\n" + ("\n".join(available) if available else "(none)")
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = "done"
                t["completed"] = datetime.now().isoformat(timespec="seconds")
                if description:
                    t["note"] = description
                _save_tasks(tasks)
                return f"Completed task #{task_id}: {t.get('description', '')}"
        return f"Error: task #{task_id} not found"

    elif action == "update":
        if task_id <= 0:
            available = [f"#{t['id']} ({t['status']}): {t.get('description', '')}" for t in tasks if t["status"] != "done"]
            return f"Error: task_id required for 'update'. Example: task_tracker(action=\"update\", task_id=1, status=\"in_progress\")\nOpen tasks:\n" + ("\n".join(available) if available else "(none)")
        if not status and not description:
            return "Error: 'update' requires at least one of: status or description"
        _VALID_STATUSES = {"open", "in_progress", "blocked", "deferred"}
        if status and status not in _VALID_STATUSES:
            return (f"Error: invalid status '{status}'. "
                    f"Use one of: open, in_progress, blocked, deferred "
                    f"(or action='done' to mark complete).")
        for t in tasks:
            if t["id"] == task_id:
                if status:
                    t["status"] = status
                if description:
                    t["note"] = description
                _save_tasks(tasks)
                return f"Updated task #{task_id}: status={t['status']}"
        return f"Error: task #{task_id} not found"

    elif action == "drop":
        if task_id <= 0:
            return "Error: task_id required for 'drop'"
        for i, t in enumerate(tasks):
            if t["id"] == task_id:
                removed = tasks.pop(i)
                _save_tasks(tasks)
                return f"Dropped task #{task_id}: {removed.get('description', '')}"
        return f"Error: task #{task_id} not found"

    elif action == "list":
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = "x" if t["status"] == "done" else " "
            line = f"[{marker}] #{t['id']} ({t['status']}): {t.get('description', '')}"
            if t.get("note"):
                line += f" — {t['note']}"
            lines.append(line)
        open_count = sum(1 for t in tasks if t["status"] != "done")
        done_count = sum(1 for t in tasks if t["status"] == "done")
        lines.append(f"\n{open_count} open, {done_count} done")
        return "\n".join(lines)

    else:
        return f"Error: unknown action '{action}'. Use: add, done, update, drop, list."


definition = {
    "type": "function",
    "function": {
        "name": "task_tracker",
        "description": (
            "Manage persistent tasks stored in .agent/state/tasks.json. "
            "Use this to track work items across turns and cycles. "
            "Actions: add (new task), done (complete), update (change status/note), "
            "drop (remove), list (show all). "
            "Tasks persist across context window resets and conversation summaries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "done", "update", "drop", "list"],
                    "description": "The operation to perform.",
                },
                "description": {
                    "type": "string",
                    "description": "Task description (for add) or note (for update/done). Optional — omit for list/done/drop.",
                },
                "task_id": {
                    "type": "integer",
                    "description": "Task ID (for done, update, drop).",
                },
                "status": {
                    "type": "string",
                    "description": "New status (for update). Common: 'in_progress', 'blocked', 'deferred'.",
                },
            },
            "required": ["action"],
        },
    },
}
