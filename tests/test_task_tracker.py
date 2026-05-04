import pytest
import json
from pathlib import Path
from tools.task_tracker import fn, _TASKS_FILE

def setup_function(function):
    # Ensure a clean tasks file for each test
    p = Path(_TASKS_FILE)
    if p.exists():
        p.unlink()

def teardown_function(function):
    # Clean up after each test
    p = Path(_TASKS_FILE)
    if p.exists():
        p.unlink()

def test_add_task():
    res = fn(action="add", description="Test task 1")
    assert "Added task #1" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert len(tasks) == 1
    assert tasks[0]["description"] == "Test task 1"
    assert tasks[0]["status"] == "open"

def test_add_task_no_description():
    res = fn(action="add")
    assert "Error: description required for 'add'" in res

def test_done_task():
    fn(action="add", description="Task to complete")
    res = fn(action="done", task_id=1)
    assert "Completed task #1" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["status"] == "done"

def test_done_task_with_note():
    fn(action="add", description="Task to complete")
    res = fn(action="done", task_id=1, description="Finished it!")
    assert "Completed task #1" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["note"] == "Finished it!"

def test_done_task_no_id():
    res = fn(action="done")
    assert "Error: task_id required for 'done'" in res

def test_done_task_not_found():
    fn(action="add", description="Task 1")
    res = fn(action="done", task_id=99)
    assert "Error: task #99 not found" in res

def test_done_auto_resolve_single():
    fn(action="add", description="The only open task")
    # No task_id provided, should auto-resolve to #1
    res = fn(action="done")
    assert "Completed task #1" in res

def test_done_auto_resolve_description():
    fn(action="add", description="Task A")
    fn(action="add", description="Task B")
    # Auto-resolve by description
    res = fn(action="done", description="Task B")
    assert "Completed task #2" in res

def test_update_status():
    fn(action="add", description="Task to update")
    res = fn(action="update", task_id=1, status="in_progress")
    assert "Updated task #1: status=in_progress" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["status"] == "in_progress"

def test_update_note():
    fn(action="add", description="Task to update")
    res = fn(action="update", task_id=1, description="New note")
    assert "Updated task #1" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["note"] == "New note"

def test_update_status_and_note():
    fn(action="add", description="Task to update")
    res = fn(action="update", task_id=1, status="blocked", description="Waiting on API")
    assert "Updated task #1: status=blocked" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["status"] == "blocked"
    assert tasks[0]["note"] == "Waiting on API"

def test_update_auto_resolve_single():
    fn(action="add", description="The only open task")
    res = fn(action="update", status="in_progress")
    assert "Updated task #1: status=in_progress" in res

def test_update_auto_resolve_description():
    fn(action="add", description="Task A")
    fn(action="add", description="Task B")
    res = fn(action="update", description="Task B", status="in_progress")
    assert "Updated task #1" not in res # Should not update Task A
    assert "Updated task #2" in res

def test_update_no_id():
    # No open tasks
    res = fn(action="update", status="in_progress")
    assert "Error: task_id required for 'update'" in res

def test_update_not_found():
    fn(action="add", description="Task 1")
    res = fn(action="update", task_id=99, status="in_progress")
    assert "Error: task #99 not found" in res

def test_update_redirect_to_done():
    fn(action="add", description="Task to finish")
    res = fn(action="update", task_id=1, status="done")
    assert "Completed task #1" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["status"] == "done"

def test_drop_task():
    fn(action="add", description="Task to drop")
    res = fn(action="drop", task_id=1)
    assert "Dropped task #1" in res
    
    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert len(tasks) == 0

def test_drop_no_id():
    res = fn(action="drop")
    assert "Error: task_id required for 'drop'" in res

def test_drop_not_found():
    fn(action="add", description="Task 1")
    res = fn(action="drop", task_id=99)
    assert "Error: task #99 not found" in res

def test_list_empty():
    res = fn(action="list")
    assert res == "No tasks."

def test_list_tasks():
    fn(action="add", description="Open task")
    fn(action="add", description="Done task")
    fn(action="done", task_id=2)
    res = fn(action="list")
    assert "[ ] #1 (open): Open task" in res
    assert "[x] #2 (done): Done task" in res
    assert "1 open, 1 done" in res

def test_list_with_notes():
    fn(action="add", description="Task with note")
    fn(action="update", task_id=1, description="Added a note")
    res = fn(action="list")
    assert "— Added a note" in res

def test_invalid_action():
    res = fn(action="invalid")
    assert "Error: unknown action 'invalid'" in res

def test_json_corruption_list_returns_error():
    """Issue #670: corrupted tasks.json must return an Error, not silently 'No tasks.'"""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("NOT JSON {{{", encoding='utf-8')

    res = fn(action="list")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "corrupted" in res.lower()


def test_json_corruption_add_returns_error():
    """Issue #670: add must not silently overwrite a corrupted file."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    original_content = "CORRUPTED DATA"
    p.write_text(original_content, encoding='utf-8')

    res = fn(action="add", description="New task after corruption")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    # The corrupted file must NOT be overwritten by the failed add
    assert p.read_text(encoding='utf-8') == original_content


def test_json_corruption_done_returns_error():
    """Issue #670: done must return Error on corrupted file, not 'task not found'."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("<<<invalid>>>", encoding='utf-8')

    res = fn(action="done", task_id=1)
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "corrupted" in res.lower()


def test_json_corruption_all_actions_return_error():
    """Issue #670: every action must return Error on a corrupted file."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{bad json", encoding='utf-8')

    for action_kwargs in [
        {"action": "list"},
        {"action": "add", "description": "x"},
        {"action": "done", "task_id": 1},
        {"action": "update", "task_id": 1, "status": "in_progress"},
        {"action": "drop", "task_id": 1},
    ]:
        res = fn(**action_kwargs)
        assert res.startswith("Error:"), (
            f"action={action_kwargs!r}: expected Error, got: {res!r}"
        )
        assert "corrupted" in res.lower(), (
            f"action={action_kwargs!r}: 'corrupted' not in response: {res!r}"
        )


# ── Issue #535: description must be optional (no KeyError when omitted) ──

def test_list_without_description_arg():
    """Calling task_tracker without description must not raise KeyError."""
    fn(action="add", description="A task")
    # Pass only action — model may omit description entirely
    res = fn(**{"action": "list"})
    assert "A task" in res


def test_done_without_description_arg():
    """action=done without description must not raise."""
    fn(action="add", description="Task to complete")
    res = fn(**{"action": "done", "task_id": 1})
    assert "Completed task #1" in res


def test_update_without_description_arg():
    """action=update without description must not raise."""
    fn(action="add", description="Task to update")
    res = fn(**{"action": "update", "task_id": 1, "status": "in_progress"})
    assert "Updated task #1" in res


def test_task_missing_description_field_list():
    """Tasks stored without 'description' key must not crash list action."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write a task record that lacks the 'description' field
    p.write_text(json.dumps([{"id": 1, "status": "open", "created": "2026-01-01T00:00:00"}]) + "\n",
                 encoding="utf-8")
    res = fn(action="list")
    assert "#1" in res
    assert "KeyError" not in res


# ── Issue #640: update rejects invalid status values ──

def test_update_invalid_status_rejected():
    """update with an unrecognised status must return an Error, not silently corrupt the task."""
    fn(action="add", description="Task for status test")
    res = fn(action="update", task_id=1, status="foobar")
    assert res.startswith("Error:")
    assert "foobar" in res
    # Task must not be modified
    import json as _json
    tasks = _json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "open"


def test_update_valid_statuses_accepted():
    """All documented valid status values must be accepted by update."""
    fn(action="add", description="Status cycling task")
    for valid_status in ("in_progress", "blocked", "deferred", "open"):
        res = fn(action="update", task_id=1, status=valid_status)
        assert "Error" not in res, f"Unexpected error for status={valid_status!r}: {res}"
        assert f"status={valid_status}" in res


def test_update_done_redirect_still_works():
    """update with status='done' or 'completed' must still redirect to the done action."""
    fn(action="add", description="Task to finish via update")
    res = fn(action="update", task_id=1, status="done")
    assert "Completed task #1" in res
    import json as _json
    tasks = _json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "done"


# ── Issue #642: update with no status/description must not silently no-op ──

def test_update_no_status_no_description_returns_error():
    """update with task_id but neither status nor description must return an Error."""
    fn(action="add", description="Task for no-op test")
    res = fn(action="update", task_id=1)
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "status" in res or "description" in res
    # Task must not be modified
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "open"
    assert "note" not in tasks[0]


def test_update_no_status_no_description_auto_resolved_returns_error():
    """update with auto-resolved task_id but no status/description must also error."""
    fn(action="add", description="Single open task")
    # auto-resolve will find task_id=1, but no status/description → must error
    res = fn(action="update")
    # Either error about missing task_id (multiple tasks not auto-resolvable) or
    # about missing status/description — either way must not say 'Updated'.
    assert "Updated" not in res


def test_update_with_only_description_still_works():
    """update with just a description note (no status) must succeed."""
    fn(action="add", description="Task to annotate")
    res = fn(action="update", task_id=1, description="Added a note")
    assert "Updated task #1" in res
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["note"] == "Added a note"
    assert tasks[0]["status"] == "open"  # status unchanged


def test_update_with_only_status_still_works():
    """update with just a status (no description) must succeed."""
    fn(action="add", description="Task to progress")
    res = fn(action="update", task_id=1, status="in_progress")
    assert "Updated task #1: status=in_progress" in res
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "in_progress"


# ── Issue #664: non-integer task_id must return Error, not raise TypeError ──

def test_update_non_integer_task_id_string():
    """update with task_id='notanumber' must return Error, not raise TypeError."""
    fn(action="add", description="test")
    res = fn(action="update", task_id="notanumber", status="in_progress")
    assert res.startswith("Error:"), f"Expected Error string, got: {res!r}"
    assert "task_id" in res.lower()


def test_done_non_integer_task_id_string():
    """done with task_id='bad' must return Error, not raise TypeError."""
    fn(action="add", description="test")
    res = fn(action="done", task_id="bad")
    assert res.startswith("Error:"), f"Expected Error string, got: {res!r}"


def test_drop_non_integer_task_id_string():
    """drop with task_id='bad' must return Error, not raise TypeError."""
    fn(action="add", description="test")
    res = fn(action="drop", task_id="bad")
    assert res.startswith("Error:"), f"Expected Error string, got: {res!r}"


def test_non_integer_task_id_dict():
    """task_id as a dict must return Error, not raise TypeError."""
    res = fn(action="done", task_id={"id": 1})
    assert res.startswith("Error:"), f"Expected Error string, got: {res!r}"


def test_non_integer_task_id_none():
    """task_id=None must return Error, not raise TypeError."""
    fn(action="add", description="test")
    res = fn(action="update", task_id=None, status="in_progress")
    assert res.startswith("Error:"), f"Expected Error string, got: {res!r}"


def test_string_numeric_task_id_coerced():
    """task_id='1' (string that looks like an int) should be coerced and work."""
    fn(action="add", description="Task to update")
    res = fn(action="update", task_id="1", status="in_progress")
    assert "Updated task #1" in res


def test_float_task_id_coerced():
    """task_id=1.0 (float) should be coerced to int and work."""
    fn(action="add", description="Task to complete")
    res = fn(action="done", task_id=1.0)
    assert "Completed task #1" in res
