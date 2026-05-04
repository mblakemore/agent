"""Task tracker tool — persistent task management via .agent/state/tasks.json."""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path


_TASKS_FILE = ".agent/state/tasks.json"

# Sentinel returned by _load_tasks when the file exists but contains invalid JSON.
# Using a named class (rather than None / [] / a string) lets fn() detect it
# unambiguously without coupling to a magic string value.
class _Corrupted:
    def __init__(self, path: str, detail: str):
        self.path = path
        self.detail = detail

    def error_msg(self) -> str:
        return (
            f"Error: tasks file is corrupted (invalid JSON): {self.path}\n"
            f"Detail: {self.detail}\n"
            "Restore from backup or delete the file to start fresh."
        )


def _load_tasks():
    """Return list-of-task-dicts, or a _Corrupted sentinel if the file is unreadable."""
    p = Path(_TASKS_FILE)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding='utf-8', errors='replace')
        # An empty file (or whitespace-only) is a valid initial state — treat it
        # the same as a missing file rather than raising JSONDecodeError.
        if not raw.strip():
            return []
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return _Corrupted(str(p.resolve()), str(exc))
    except IOError as exc:
        return _Corrupted(str(p.resolve()), str(exc))


def get_tasks():
    """Return the current list of tasks, or [] if the file is missing/corrupted."""
    result = _load_tasks()
    if isinstance(result, _Corrupted):
        return []
    return result


def _save_tasks(tasks):
    """Atomically write tasks to disk using a temp-file + rename pattern.

    This prevents a killed/interrupted write from leaving a partial (corrupted)
    JSON file behind — the old file is only replaced once the new one is fully
    flushed to disk.
    """
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file in the same directory so os.replace() is
    # guaranteed to be atomic on POSIX (same filesystem, single syscall).
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(tasks, indent=2) + "\n")
        os.replace(tmp_path, str(p))
    except Exception:
        # Clean up the temp file if anything goes wrong before the rename.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
    # or passes a non-string (e.g. integer) — coerce to str to prevent AttributeError
    # from .strip() calls further down.  Strip whitespace so that a
    # whitespace-only string ("   ") is treated the same as an empty string.
    if not isinstance(description, str):
        description = str(description) if description is not None else ""
    description = description.strip()
    # Collapse embedded newlines/tabs (and any adjacent spaces) into a single
    # space so that descriptions and notes remain single-line.  This prevents
    # the `list` output — which formats one task per line — from being broken
    # by a multi-line or tab-containing description stored verbatim in the JSON.
    # e.g. "line1\nline2" → "line1 line2",  "a\tb" → "a b"
    # Tabs were not handled by the original newline-only guard (#728).
    if "\n" in description or "\r" in description or "\t" in description:
        description = re.sub(r" *[\t\n\r][ \t\n\r]*", " ", description).strip()

    # Validate task_id type — must be an integer (or the default 0).
    # Non-integer values (e.g. strings passed by a model) would cause
    # TypeError at the `task_id <= 0` comparisons below.
    # Float task_ids with a fractional part (e.g. 1.5, 6.9) must be rejected
    # rather than silently truncated — int(1.5) == 1, which would operate on
    # the wrong task.  Whole-number floats (e.g. 2.0) are safe to coerce.
    if not isinstance(task_id, int):
        try:
            coerced = int(task_id)
            if isinstance(task_id, float) and task_id != coerced:
                return (
                    f"Error: task_id must be an integer, got non-integer float: {task_id!r}. "
                    f"Did you mean {coerced} or {coerced + 1}?"
                )
            task_id = coerced
        except (TypeError, ValueError):
            return f"Error: task_id must be an integer, got {type(task_id).__name__!r}: {task_id!r}"

    tasks = _load_tasks()
    if isinstance(tasks, _Corrupted):
        return tasks.error_msg()

    # Treat update with completed/done status as the "done" action
    if action == "update" and status in ("completed", "done"):
        action = "done"

    # Auto-resolve task_id: if missing, try to find a unique open task.
    # Track whether description was consumed solely as a selector so it is NOT
    # also stored as a note — the caller used it to identify the task, not annotate it.
    _description_used_for_resolution = False
    if action in ("done", "update", "drop") and task_id <= 0:
        open_tasks = [t for t in tasks if t["status"] not in ("done", "completed")]
        if len(open_tasks) == 1:
            task_id = open_tasks[0]["id"]
        elif description:
            desc_lower = description.lower()
            # Prefer exact match first — resolves unambiguously even when the
            # description is a substring of another task's description.
            exact = [t for t in open_tasks if t.get("description", "").lower() == desc_lower]
            if len(exact) == 1:
                task_id = exact[0]["id"]
                _description_used_for_resolution = True
            else:
                # Fall back to fuzzy/substring match
                matches = [t for t in open_tasks if desc_lower in t.get("description", "").lower()
                           or t.get("description", "").lower() in desc_lower]
                if len(matches) == 1:
                    task_id = matches[0]["id"]
                    _description_used_for_resolution = True

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
            available = [f"#{t['id']} ({t['status']}): {t.get('description', '')}" for t in tasks if t["status"] not in ("done", "completed")]
            return f"Error: task_id required for 'done'. Example: task_tracker(action=\"done\", task_id=1)\nOpen tasks:\n" + ("\n".join(available) if available else "(none)")
        for t in tasks:
            if t["id"] == task_id:
                if t["status"] in ("done", "completed"):
                    return f"Error: task #{task_id} is already done"
                t["status"] = "done"
                t["completed"] = datetime.now().isoformat(timespec="seconds")
                if description and not _description_used_for_resolution:
                    t["note"] = description
                _save_tasks(tasks)
                return f"Completed task #{task_id}: {t.get('description', '')}"
        return f"Error: task #{task_id} not found"

    elif action == "update":
        if task_id <= 0:
            available = [f"#{t['id']} ({t['status']}): {t.get('description', '')}" for t in tasks if t["status"] not in ("done", "completed")]
            return f"Error: task_id required for 'update'. Example: task_tracker(action=\"update\", task_id=1, status=\"in_progress\")\nOpen tasks:\n" + ("\n".join(available) if available else "(none)")
        # When description was used only to resolve task_id, it carries no note
        # intent — treat it as absent for validation and note-writing purposes.
        _effective_description = "" if _description_used_for_resolution else description
        if not status and not _effective_description:
            return "Error: 'update' requires at least one of: status or description"
        _VALID_STATUSES = {"open", "in_progress", "blocked", "deferred"}
        if status and status not in _VALID_STATUSES:
            return (f"Error: invalid status '{status}'. "
                    f"Use one of: open, in_progress, blocked, deferred "
                    f"(or action='done' to mark complete).")
        for t in tasks:
            if t["id"] == task_id:
                if t["status"] in ("done", "completed"):
                    return f"Error: task #{task_id} is already done"
                if status:
                    t["status"] = status
                if _effective_description:
                    t["note"] = _effective_description
                _save_tasks(tasks)
                msg = f"Updated task #{task_id}: status={t['status']}"
                if _effective_description:
                    msg += f", note={_effective_description!r}"
                return msg
        return f"Error: task #{task_id} not found"

    elif action == "drop":
        if task_id <= 0:
            available = [f"#{t['id']} ({t['status']}): {t.get('description', '')}" for t in tasks if t["status"] not in ("done", "completed")]
            return f"Error: task_id required for 'drop'. Example: task_tracker(action=\"drop\", task_id=1)\nOpen tasks:\n" + ("\n".join(available) if available else "(none)")
        for i, t in enumerate(tasks):
            if t["id"] == task_id:
                removed = tasks.pop(i)
                _save_tasks(tasks)
                return f"Dropped task #{task_id}: {removed.get('description', '')}"
        return f"Error: task #{task_id} not found"

    elif action == "list":
        if not tasks:
            return "No tasks."
        # Apply optional status filter (case-insensitive).
        # When status is provided, only include tasks whose status matches.
        status_filter = status.strip().lower() if status else ""
        if status_filter:
            filtered = [t for t in tasks if t["status"].lower() == status_filter]
            if not filtered:
                return f"No tasks with status '{status_filter}'."
        else:
            filtered = tasks
        _DONE_STATUSES = {"done", "completed"}
        lines = []
        for t in filtered:
            marker = "x" if t["status"] in _DONE_STATUSES else " "
            line = f"[{marker}] #{t['id']} ({t['status']}): {t.get('description', '')}"
            if t.get("note"):
                line += f" — {t['note']}"
            lines.append(line)
        # Summary counts always reflect the full task list, not just the filtered view.
        # Both 'done' and 'completed' are terminal statuses (#738).
        # Count each non-done status separately so the summary is accurate — lumping
        # in_progress/blocked/deferred into "open" would misreport the true breakdown (#748).
        _ACTIVE_STATUSES = ("in_progress", "blocked", "deferred")
        open_count = sum(1 for t in tasks if t["status"] == "open")
        done_count = sum(1 for t in tasks if t["status"] in _DONE_STATUSES)
        active_counts = {s: sum(1 for t in tasks if t["status"] == s) for s in _ACTIVE_STATUSES}
        parts = [f"{open_count} open"]
        for s in _ACTIVE_STATUSES:
            if active_counts[s]:
                parts.append(f"{active_counts[s]} {s}")
        parts.append(f"{done_count} done")
        lines.append("\n" + ", ".join(parts))
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
