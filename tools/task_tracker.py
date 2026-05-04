"""Task tracker tool — persistent task management via .agent/state/tasks.json."""

import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path


_TASKS_FILE = ".agent/state/tasks.json"

# Module-level lock that serialises the load → mutate → save cycle for all
# write actions (add, done, update, drop).  Without this lock two concurrent
# callers can each load a stale snapshot and the last writer silently
# overwrites the other's changes — a TOCTOU / lost-update data-loss bug.
# A threading.Lock is sufficient for in-process concurrency; cross-process
# safety would require an advisory fcntl lock, but the agent runs as a single
# process so a threading.Lock covers the real use-case.
_write_lock = threading.Lock()

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
    try:
        exists = p.exists()
    except PermissionError:
        return _Corrupted(str(p), "permission denied reading task file directory")
    if not exists:
        return []
    try:
        raw = p.read_text(encoding='utf-8-sig', errors='replace')
        # An empty file (or whitespace-only) is a valid initial state — treat it
        # the same as a missing file rather than raising JSONDecodeError.
        if not raw.strip():
            return []
        data = json.loads(raw)
        # The file must contain a JSON array, not a dict, null, or scalar.
        # A null value (JSON `null` → Python `None`) or a non-list type means the
        # file has wrong structure — treat it as corrupted so we surface a clear
        # error rather than crashing on iteration or silently returning "No tasks."
        if not isinstance(data, list):
            return _Corrupted(
                str(p.resolve()),
                f"expected a JSON array at top level, got {type(data).__name__}",
            )
        # Each element must be a dict.  Non-dict elements (strings, ints, etc.)
        # would cause AttributeError / TypeError when the code does t["status"].
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                return _Corrupted(
                    str(p.resolve()),
                    f"element {i} is not an object (got {type(item).__name__})",
                )
        return data
    except json.JSONDecodeError as exc:
        return _Corrupted(str(p.resolve()), str(exc))
    except (IOError, OSError) as exc:
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

    Returns None on success, or an error string if the write fails.
    """
    p = Path(_TASKS_FILE)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return f"Error: cannot write to task file: permission denied: {_TASKS_FILE}"
    # Write to a sibling temp file in the same directory so os.replace() is
    # guaranteed to be atomic on POSIX (same filesystem, single syscall).
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    except PermissionError:
        return f"Error: cannot write to task file: permission denied: {_TASKS_FILE}"
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(tasks, indent=2) + "\n")
        os.replace(tmp_path, str(p))
    except PermissionError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return f"Error: cannot write to task file: permission denied: {_TASKS_FILE}"
    except Exception:
        # Clean up the temp file if anything goes wrong before the rename.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return None


def _next_id(tasks):
    return max((t.get("id", 0) for t in tasks), default=0) + 1


def fn(action: str, description: str = "", task_id: int = 0, status: str = "", limit: int = 0) -> str:
    """Manage persistent tasks.

    Args:
        action: One of "add", "done", "update", "drop", "list".
        description: Task description (for add) or note (for update). Optional — omit for list/done/drop.
        task_id: Task ID (for done, update, drop).
        status: New status string (for update). Common: "in_progress", "blocked", "deferred".
        limit: For "list": maximum number of tasks to return (0 = no limit).
    """
    # Validate action type and content before anything else.
    if not isinstance(action, str):
        return f"Error: action must be a string, got {type(action).__name__!r}"
    if '\x00' in action:
        return "Error: action contains a null byte, which is not allowed"

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
    if '\x00' in description:
        return "Error: description contains a null byte, which is not allowed"
    # Reject excessively long descriptions — unbounded strings would exhaust
    # disk space and inflate the context window when listed (#809).
    _MAX_DESCRIPTION_LEN = 2000
    if len(description) > _MAX_DESCRIPTION_LEN:
        return (
            f"Error: description is too long ({len(description)} chars). "
            f"Maximum allowed length is {_MAX_DESCRIPTION_LEN} characters."
        )

    # Validate task_id type — must be an integer (or the default 0).
    # Non-integer values (e.g. strings passed by a model) would cause
    # TypeError at the `task_id <= 0` comparisons below.
    # Float task_ids with a fractional part (e.g. 1.5, 6.9) must be rejected
    # rather than silently truncated — int(1.5) == 1, which would operate on
    # the wrong task.  Whole-number floats (e.g. 2.0) are safe to coerce.
    # Booleans are a subclass of int in Python; True==1 and False==0, so
    # task_id=True would silently operate on task #1 and task_id=False would
    # be treated as 0 (no task specified) — both are wrong.  Reject explicitly.
    if isinstance(task_id, bool):
        return (
            f"Error: task_id must be a plain integer, got bool ({task_id!r}). "
            f"Pass an integer task ID (e.g. task_id=1)."
        )
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

    # Validate status type — must be a string (or the default "").
    # Non-string values (e.g. an integer passed by a model) would cause
    # AttributeError at `status.strip()` in the list action (#853).
    # Booleans are a subclass of str in no language but a common mistake —
    # reject them explicitly with a clear message.  None is treated as "".
    if isinstance(status, bool):
        return (
            f"Error: status must be a string, got bool ({status!r}). "
            f"Pass a status string such as 'open', 'in_progress', 'blocked', 'deferred'."
        )
    if status is None:
        status = ""
    if not isinstance(status, str):
        return (
            f"Error: status must be a string, got {type(status).__name__} ({status!r}). "
            f"Pass a status string such as 'open', 'in_progress', 'blocked', 'deferred'."
        )
    if '\x00' in status:
        return "Error: status contains a null byte, which is not allowed"

    # Validate limit
    if isinstance(limit, bool):
        return (
            f"Error: limit must be a plain integer, got bool ({limit!r}). "
            f"Pass an integer (e.g. limit=10) or omit for no limit."
        )
    if isinstance(limit, str):
        return (
            f"Error: limit must be an integer, got 'str': {limit!r}. "
            f"Pass an integer without quotes (e.g. limit=10)."
        )
    if not isinstance(limit, int):
        try:
            coerced = int(limit)
            if isinstance(limit, float) and limit != coerced:
                return (
                    f"Error: limit must be an integer, got non-integer float: {limit!r}. "
                    f"Did you mean {coerced} or {coerced + 1}?"
                )
            limit = coerced
        except (TypeError, ValueError):
            return f"Error: limit must be an integer, got {type(limit).__name__!r}: {limit!r}"
    if limit < 0:
        return f"Error: limit must be >= 0 (got {limit}). Pass 0 for no limit."

    # list is read-only — load without holding the lock.
    # All write actions (add, done, update, drop) must hold _write_lock for
    # the entire load → mutate → save sequence to prevent lost-update races
    # when two callers run concurrently (#811).
    if action == "list":
        tasks = _load_tasks()
        if isinstance(tasks, _Corrupted):
            return tasks.error_msg()
        # Validate the status filter before anything else so that an unknown
        # status value returns an error even when the task list is empty.
        status_filter = status.strip().lower() if status else ""
        # Valid status values: the four mutable statuses plus the two terminal ones.
        _ALL_VALID_STATUSES = {"open", "in_progress", "blocked", "deferred", "done", "completed"}
        if status_filter and status_filter not in _ALL_VALID_STATUSES:
            return (
                f"Error: unknown status filter '{status_filter}'. "
                f"Valid values: open, in_progress, blocked, deferred, done, completed."
            )
        if not tasks:
            return "No tasks."
        # Apply optional status filter (case-insensitive).
        # When status is provided, only include tasks whose status matches.
        if status_filter:
            filtered = [t for t in tasks if t.get("status", "").lower() == status_filter]
            if not filtered:
                return f"No tasks with status '{status_filter}'."
        else:
            filtered = tasks
        _DONE_STATUSES = {"done", "completed"}
        # Apply limit to the filtered view (0 means no limit)
        display = filtered[:limit] if limit > 0 else filtered
        lines = []
        for t in display:
            marker = "x" if t["status"] in _DONE_STATUSES else " "
            line = f"[{marker}] #{t['id']} ({t['status']}): {t.get('description', '')}"
            if t.get("note"):
                line += f" — {t['note']}"
            lines.append(line)
        if limit > 0 and len(filtered) > limit:
            lines.append(f"(showing {limit} of {len(filtered)} tasks)")
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

    elif action not in ("add", "done", "update", "drop"):
        return f"Error: unknown action '{action}'. Use: add, done, update, drop, list."

    # ── Write actions — serialised under _write_lock ──────────────────────────
    # Acquire the lock before _load_tasks() so the entire read-modify-write
    # sequence is atomic with respect to other threads.  Without the lock two
    # concurrent "add" calls each load a stale snapshot and the last writer
    # silently overwrites the first's task (TOCTOU / lost-update, #811).
    with _write_lock:
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
                # If the caller passed a description that exactly matches the resolved
                # task's own description, they were identifying it — not annotating it.
                # Mark it consumed-for-resolution so the note is NOT stored.
                if description and description.strip().lower() == open_tasks[0].get("description", "").strip().lower():
                    _description_used_for_resolution = True
            elif description:
                desc_lower = description.lower()
                # Prefer exact match first — resolves unambiguously even when the
                # description is a substring of another task's description.
                exact = [t for t in open_tasks if t.get("description", "").lower() == desc_lower]
                if len(exact) == 1:
                    task_id = exact[0]["id"]
                    _description_used_for_resolution = True
                elif len(exact) > 1:
                    # Multiple exact matches (should not happen due to duplicate guard on add,
                    # but handle defensively).
                    match_list = ", ".join(f"#{t['id']} {t.get('description', '')!r}" for t in exact)
                    return (
                        f"Error: {len(exact)} tasks match {description!r} exactly — "
                        f"use task_id to specify: {match_list}"
                    )
                else:
                    # Fall back to fuzzy/substring match
                    matches = [t for t in open_tasks if desc_lower in t.get("description", "").lower()
                               or t.get("description", "").lower() in desc_lower]
                    if len(matches) == 1:
                        task_id = matches[0]["id"]
                        _description_used_for_resolution = True
                    elif len(matches) > 1:
                        match_list = ", ".join(
                            f"#{t['id']} {t.get('description', '')!r}" for t in matches
                        )
                        return (
                            f"Error: {len(matches)} tasks match {description!r} — "
                            f"use task_id to specify: {match_list}"
                        )

        if action == "add":
            # task_id is not a valid parameter for 'add' — IDs are assigned automatically.
            # Return an error rather than silently ignoring it.
            if task_id > 0:
                return (
                    f"Error: task_id={task_id} is not valid for 'add'. "
                    f"Task IDs are assigned automatically. "
                    f"To update an existing task, use action='update' with task_id={task_id}."
                )
            if not description:
                if status and task_id > 0:
                    return (f"Error: 'add' requires description. To change status of an "
                            f"existing task, use action='update' with task_id={task_id}, status='{status}'.")
                if status:
                    return (f"Error: 'add' requires description. To set status on an "
                            f"existing task, use action='update' with task_id=<N>, status='{status}'.")
                return "Error: description required for 'add'"
            # Reject status= on add — new tasks always start as 'open'.
            # Valid mutable statuses for reference; 'open' is the only allowed initial state.
            _ADD_VALID_INITIAL = {"open"}
            _ALL_MUTABLE_STATUSES = {"open", "in_progress", "blocked", "deferred"}
            if status:
                if status not in _ADD_VALID_INITIAL:
                    if status in _ALL_MUTABLE_STATUSES:
                        return (
                            f"Error: new tasks always start as 'open'. "
                            f"To add a task and immediately set status='{status}', "
                            f"use action='add' first, then action='update' with status='{status}'."
                        )
                    else:
                        return (
                            f"Error: invalid status '{status}' for 'add'. "
                            f"New tasks always start as 'open'. "
                            f"Use action='update' after adding to change status."
                        )
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
            err = _save_tasks(tasks)
            if err:
                return err
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
                    err = _save_tasks(tasks)
                    if err:
                        return err
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
                    err = _save_tasks(tasks)
                    if err:
                        return err
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
                    if t["status"] in ("done", "completed"):
                        return f"Error: task #{task_id} is already done; use action='list' to review or leave as-is to preserve history"
                    removed = tasks.pop(i)
                    err = _save_tasks(tasks)
                    if err:
                        return err
                    return f"Dropped task #{task_id}: {removed.get('description', '')}"
            return f"Error: task #{task_id} not found"


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
                "limit": {
                    "type": "integer",
                    "description": "For 'list': maximum number of tasks to return. 0 (default) means no limit.",
                    "default": 0,
                },
            },
            "required": ["action"],
        },
    },
}
