import os
import shutil
import tempfile
import pytest
import json
from pathlib import Path
import tools.task_tracker as _tt_mod
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
    # Return message must confirm both the (unchanged) status and the saved note
    assert "Updated task #1: status=open" in res
    assert "note='New note'" in res

    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["note"] == "New note"

def test_update_status_and_note():
    fn(action="add", description="Task to update")
    res = fn(action="update", task_id=1, status="blocked", description="Waiting on API")
    assert "Updated task #1: status=blocked" in res
    assert "note='Waiting on API'" in res

    with open(_TASKS_FILE, 'r') as f:
        tasks = json.load(f)
    assert tasks[0]["status"] == "blocked"
    assert tasks[0]["note"] == "Waiting on API"

def test_update_status_only_no_note_in_message():
    """Status-only update must NOT include a note= component in the message."""
    fn(action="add", description="Task to update")
    res = fn(action="update", task_id=1, status="in_progress")
    assert "Updated task #1: status=in_progress" in res
    assert "note=" not in res

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


# ── Issue #742: drop with missing task_id must include example and task list hint ──

def test_drop_missing_task_id_includes_example():
    """drop without task_id must include an example invocation in the error (#742)."""
    fn(action="add", description="Task A")
    fn(action="add", description="Task B")
    res = fn(action="drop")
    assert "Error: task_id required for 'drop'" in res, f"Expected error, got: {res!r}"
    assert 'task_tracker(action="drop", task_id=1)' in res, (
        f"Error must include an example invocation, got: {res!r}"
    )


def test_drop_missing_task_id_includes_open_tasks_list():
    """drop without task_id must list the open tasks so the agent can pick one (#742)."""
    fn(action="add", description="Task A")
    fn(action="add", description="Task B")
    res = fn(action="drop")
    assert "Open tasks:" in res, f"Expected 'Open tasks:' hint section, got: {res!r}"
    assert "#1" in res, f"Task #1 must appear in hint, got: {res!r}"
    assert "#2" in res, f"Task #2 must appear in hint, got: {res!r}"


def test_drop_missing_task_id_excludes_done_tasks_from_hint():
    """drop hint list must not include done/completed tasks (#742)."""
    from pathlib import Path
    import json as _json
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "legacy completed", "status": "completed", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "still open A", "status": "open", "created": "2024-01-01T00:00:00"},
        {"id": 3, "description": "still open B", "status": "open", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(_json.dumps(tasks))
    res = fn(action="drop")
    assert "task_id required" in res, f"Expected task_id-required error, got: {res!r}"
    assert "#1" not in res, (
        f"'Open tasks:' hint must not include completed task #1, got: {res!r}"
    )
    assert "#2" in res, f"'Open tasks:' hint must include open task #2, got: {res!r}"
    assert "#3" in res, f"'Open tasks:' hint must include open task #3, got: {res!r}"


def test_drop_missing_task_id_shows_none_when_no_open_tasks():
    """drop hint list shows '(none)' when only done/completed tasks exist (#742)."""
    from pathlib import Path
    import json as _json
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "finished task", "status": "done", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(_json.dumps(tasks))
    res = fn(action="drop")
    assert "(none)" in res, (
        f"Expected '(none)' when no open tasks remain, got: {res!r}"
    )
    assert "#1" not in res, (
        f"done task #1 must not appear in hint, got: {res!r}"
    )


def test_drop_no_id_no_tasks_shows_none():
    """drop with no tasks at all should show '(none)' in the hint (#742)."""
    res = fn(action="drop")
    assert "Error: task_id required for 'drop'" in res
    assert "(none)" in res, f"Expected '(none)' for empty task list, got: {res!r}"


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


# ── wrong-type description tests (#680) ───────────────────────────────────────

def test_add_integer_description_coerced():
    """description=42 (int) must be coerced to '42' and succeed, not raise AttributeError (#680)."""
    res = fn(action="add", description=42)
    assert "Added task" in res
    assert "42" in res


def test_add_float_description_coerced():
    """description=3.14 (float) must be coerced to '3.14' and succeed (#680)."""
    res = fn(action="add", description=3.14)
    assert "Added task" in res
    assert "3.14" in res


def test_add_none_description_treated_as_empty():
    """description=None must be treated as empty (same as omitting it), returning an error (#680)."""
    res = fn(action="add", description=None)
    assert "Error" in res


def test_add_list_description_coerced():
    """description=['a', 'b'] (list) must be coerced to string and succeed (#680)."""
    res = fn(action="add", description=['a', 'b'])
    assert "Added task" in res


# ── Issue #692: whitespace-only description must be rejected like empty ──────

def test_add_whitespace_only_description_rejected():
    """description='   ' (spaces only) must return Error, not create a blank task (#692)."""
    res = fn(action="add", description="   ")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "description required" in res


def test_add_tab_only_description_rejected():
    """description='\t\t' (tabs only) must return Error, not create a blank task (#692)."""
    res = fn(action="add", description="\t\t")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "description required" in res


def test_add_newline_only_description_rejected():
    """description='\n' (newline only) must return Error, not create a blank task (#692)."""
    res = fn(action="add", description="\n")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "description required" in res


def test_add_description_stripped_of_surrounding_whitespace():
    """description with surrounding whitespace is stripped before storage (#692)."""
    res = fn(action="add", description="  real task  ")
    assert "Added task #1: real task" == res, f"Unexpected: {res!r}"
    # Verify stored description is stripped
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["description"] == "real task"


# ── Issue #704: done action must reject already-completed tasks ──────────────

def test_done_on_already_done_task_returns_error():
    """done on an already-done task must return Error, not silent success (#704)."""
    fn(action="add", description="Task to complete")
    fn(action="done", task_id=1)
    res = fn(action="done", task_id=1)
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "already done" in res


def test_done_on_already_done_task_does_not_overwrite_timestamp():
    """done on an already-done task must not overwrite the completed timestamp (#704)."""
    fn(action="add", description="Task")
    fn(action="done", task_id=1)
    tasks_after_first = json.loads(Path(_TASKS_FILE).read_text())
    first_ts = tasks_after_first[0]["completed"]

    fn(action="done", task_id=1)  # second call — should be rejected
    tasks_after_second = json.loads(Path(_TASKS_FILE).read_text())
    second_ts = tasks_after_second[0]["completed"]

    assert first_ts == second_ts, "completed timestamp must not be overwritten by a duplicate done"


def test_done_on_open_task_still_works_after_fix():
    """Completing an open task must still succeed after the guard is in place (#704)."""
    fn(action="add", description="Normal task")
    res = fn(action="done", task_id=1)
    assert "Completed task #1" in res
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "done"


# ── Issue #706: update action must not reopen completed tasks ─────────────────

def test_update_on_done_task_returns_error():
    """update on a done task must return Error, not silently reopen it (#706)."""
    fn(action="add", description="Task to complete")
    fn(action="done", task_id=1)
    res = fn(action="update", task_id=1, status="in_progress")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "already done" in res


def test_update_on_done_task_does_not_change_status():
    """update on a done task must not modify the stored status (#706)."""
    fn(action="add", description="Task")
    fn(action="done", task_id=1)
    fn(action="update", task_id=1, status="in_progress")  # must be rejected
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "done", "status must remain 'done' after rejected update"


def test_update_on_done_task_does_not_add_note():
    """update with description on a done task must not add a note (#706)."""
    fn(action="add", description="Task")
    fn(action="done", task_id=1)
    fn(action="update", task_id=1, description="spurious note")
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert "note" not in tasks[0], "note must not be added to a completed task by a rejected update"


def test_update_on_done_task_cannot_be_done_again():
    """After a rejected update on a done task the task must still be done (#706)."""
    fn(action="add", description="Task")
    fn(action="done", task_id=1)
    fn(action="update", task_id=1, status="in_progress")  # rejected
    # done a second time must also be rejected (not silently succeed)
    res2 = fn(action="done", task_id=1)
    assert res2.startswith("Error:"), f"Expected Error on second done, got: {res2!r}"
    assert "already done" in res2


def test_update_on_open_task_still_works_after_706_fix():
    """update on an open task must still succeed after the #706 guard is added."""
    fn(action="add", description="Open task")
    res = fn(action="update", task_id=1, status="in_progress")
    assert "Updated task #1: status=in_progress" in res
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "in_progress"


# ── Issue #714: empty tasks file must be treated as empty list, not corrupted ──

def test_empty_file_list_returns_no_tasks():
    """list on a zero-byte tasks file must return 'No tasks.', not a corruption error (#714)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")

    res = fn(action="list")
    assert res == "No tasks.", f"Expected 'No tasks.', got: {res!r}"


def test_empty_file_add_succeeds():
    """add on a zero-byte tasks file must create a new task, not error (#714)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")

    res = fn(action="add", description="first task")
    assert "Added task #1: first task" == res, f"Unexpected: {res!r}"
    tasks = json.loads(p.read_text())
    assert len(tasks) == 1
    assert tasks[0]["description"] == "first task"


def test_whitespace_only_file_treated_as_empty():
    """A file containing only whitespace/newlines must also be treated as empty (#714)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n\n   \n", encoding="utf-8")

    res = fn(action="list")
    assert res == "No tasks.", f"Expected 'No tasks.', got: {res!r}"


def test_corrupted_file_still_errors():
    """Genuinely corrupted JSON must still return an Error after the #714 fix."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")

    res = fn(action="list")
    assert res.startswith("Error:"), f"Expected Error for corrupted file, got: {res!r}"
    assert "corrupted" in res.lower()


# ── Issue #716: description used for auto-resolve must not be stored as a note ──

def test_done_auto_resolve_by_description_does_not_add_note():
    """done auto-resolved via description substring must NOT store the description as a note (#716)."""
    fn(action="add", description="Fix the login bug")
    fn(action="add", description="Update the README")
    # description is used only to identify the task, not as an annotation
    res = fn(action="done", description="Fix the login bug")
    assert "Completed task #1" in res

    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert "note" not in tasks[0], (
        f"note must not be stored when description was used for auto-resolution, got: {tasks[0]!r}"
    )


def test_update_auto_resolve_by_description_does_not_add_note():
    """update auto-resolved via description substring must NOT store the description as a note (#716)."""
    fn(action="add", description="Task A")
    fn(action="add", description="Task B")
    # description='Task B' identifies the task; status is the real change
    res = fn(action="update", description="Task B", status="in_progress")
    assert "Updated task #2" in res
    assert "note=" not in res, f"note= must not appear in response when description used for resolution: {res!r}"

    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert "note" not in tasks[1], (
        f"note must not be stored when description was used for auto-resolution, got: {tasks[1]!r}"
    )


def test_done_explicit_task_id_with_description_stores_note():
    """done with explicit task_id and description MUST still store the description as a note (#716)."""
    fn(action="add", description="Refactor the API")
    res = fn(action="done", task_id=1, description="Used the new pattern")
    assert "Completed task #1" in res

    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0].get("note") == "Used the new pattern", (
        f"note must be stored when task_id is explicit: {tasks[0]!r}"
    )


def test_update_explicit_task_id_with_description_stores_note():
    """update with explicit task_id and description MUST still store the description as a note (#716)."""
    fn(action="add", description="Deploy the fix")
    res = fn(action="update", task_id=1, description="Blocked by infra issue", status="blocked")
    assert "Updated task #1" in res
    assert "note='Blocked by infra issue'" in res

    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0].get("note") == "Blocked by infra issue", (
        f"note must be stored when task_id is explicit: {tasks[0]!r}"
    )


def test_update_auto_resolve_single_open_task_with_explicit_note():
    """When auto-resolved via single-task shortcut (no description needed), an explicit
    description+task_id-less call that provides description as a note-only update should
    still work correctly when task_id is provided explicitly (#716)."""
    fn(action="add", description="The only task")
    # Single open task auto-resolve (description not used for matching here)
    res = fn(action="update", task_id=1, description="A real annotation", status="in_progress")
    assert "Updated task #1" in res
    assert "note='A real annotation'" in res

    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0].get("note") == "A real annotation"


# ── Issue #722: exact description match must take priority over substring match ──

def test_done_exact_match_preferred_over_substring():
    """done with description='fix bug' must resolve to the exact-match task even when
    another task's description contains 'fix bug' as a substring (#722)."""
    fn(action="add", description="fix bug")
    fn(action="add", description="fix bug in login")
    # 'fix bug' is an exact match for task #1 and a substring of task #2
    res = fn(action="done", description="fix bug")
    assert "Completed task #1" in res, (
        f"Expected exact-match task #1 to be completed, got: {res!r}"
    )
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "done", "task #1 must be marked done"
    assert tasks[1]["status"] == "open", "task #2 must remain open"


def test_update_exact_match_preferred_over_substring():
    """update with description='fix bug' must resolve to the exact-match task, not error
    on ambiguity, even when another task contains 'fix bug' as a substring (#722)."""
    fn(action="add", description="fix bug")
    fn(action="add", description="fix bug in login")
    res = fn(action="update", description="fix bug", status="in_progress")
    assert "Updated task #1" in res, (
        f"Expected exact-match task #1 to be updated, got: {res!r}"
    )
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "in_progress"
    assert tasks[1]["status"] == "open"


def test_drop_exact_match_preferred_over_substring():
    """drop with description='fix bug' must resolve to the exact-match task (#722)."""
    fn(action="add", description="fix bug")
    fn(action="add", description="fix bug in login")
    res = fn(action="drop", description="fix bug")
    assert "Dropped task #1" in res, (
        f"Expected exact-match task #1 to be dropped, got: {res!r}"
    )
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert len(tasks) == 1
    assert tasks[0]["description"] == "fix bug in login"


def test_exact_match_resolves_uniquely_add_prevents_duplicates():
    """add prevents duplicate open tasks; exact match therefore always resolves to one task (#722)."""
    res1 = fn(action="add", description="fix bug")
    res2 = fn(action="add", description="fix bug")
    # Second add must be rejected as a duplicate
    assert "Already exists" in res2 or "already exists" in res2, (
        f"Expected duplicate add to be rejected, got: {res2!r}"
    )
    # Exactly one task with 'fix bug' — done must succeed
    res = fn(action="done", description="fix bug")
    assert "Completed task" in res, (
        f"Expected task to be completed, got: {res!r}"
    )


def test_substring_match_still_works_when_no_exact_match():
    """done by description must still auto-resolve via substring when no exact match (#722)."""
    fn(action="add", description="fix the big login bug")
    fn(action="add", description="update docs")
    # 'login bug' is not an exact match for either task, but is a substring of task #1
    res = fn(action="done", description="login bug")
    assert "Completed task #1" in res, (
        f"Expected task #1 to be completed via substring match, got: {res!r}"
    )


# ── Issue #726: embedded newlines in description/note must be collapsed ────────

def test_add_description_with_embedded_newline_collapsed():
    """description with embedded newlines must be collapsed to a single-line string (#726)."""
    res = fn(action="add", description="line1\nline2\nline3")
    assert "Added task #1" in res
    # Stored description must contain no newlines
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert "\n" not in tasks[0]["description"], (
        f"description must not contain newlines, got: {tasks[0]['description']!r}"
    )
    assert tasks[0]["description"] == "line1 line2 line3"


def test_add_description_with_carriage_return_collapsed():
    """description with \\r\\n line endings must also be collapsed (#726)."""
    res = fn(action="add", description="part1\r\npart2")
    assert "Added task #1" in res
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert "\r" not in tasks[0]["description"]
    assert "\n" not in tasks[0]["description"]
    assert tasks[0]["description"] == "part1 part2"


def test_list_output_not_broken_by_multiline_description():
    """list output must be one task per line even when description contained newlines (#726)."""
    fn(action="add", description="first\nsecond\nthird")
    result = fn(action="list")
    # Every line in the output that contains '#1' must be a well-formed task entry
    task_lines = [line for line in result.splitlines() if "#1" in line]
    assert len(task_lines) == 1, (
        f"Expected exactly one line mentioning #1 in list output, got: {result!r}"
    )
    assert "[ ] #1 (open): first second third" in result


def test_update_note_with_embedded_newline_collapsed():
    """note passed to update with embedded newlines must be collapsed (#726)."""
    fn(action="add", description="Task to annotate")
    res = fn(action="update", task_id=1, description="note line1\nnote line2")
    assert "Updated task #1" in res
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    note = tasks[0].get("note", "")
    assert "\n" not in note, f"note must not contain newlines, got: {note!r}"
    assert note == "note line1 note line2"


def test_done_note_with_embedded_newline_collapsed():
    """note passed to done with embedded newlines must be collapsed (#726)."""
    fn(action="add", description="Task to finish")
    res = fn(action="done", task_id=1, description="finished\nwith note")
    assert "Completed task #1" in res
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    note = tasks[0].get("note", "")
    assert "\n" not in note, f"note must not contain newlines, got: {note!r}"
    assert note == "finished with note"


def test_add_newline_only_description_still_rejected():
    """description='\\n' (newline only) must still return Error after the collapse fix (#726)."""
    res = fn(action="add", description="\n")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "description required" in res


def test_add_multiline_whitespace_description_still_rejected():
    """description='  \\n  ' (whitespace + newlines only) must still return Error (#726)."""
    res = fn(action="add", description="  \n  \n  ")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "description required" in res


# ── Issue #728: embedded tab characters in description/note must be collapsed ──

def test_add_description_with_tab_collapsed():
    """description with embedded tab must be collapsed to a space (#728)."""
    res = fn(action="add", description="task\twith\ttab")
    assert "Added task #1" in res, f"Expected add success, got: {res!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert "\t" not in tasks[0]["description"], (
        f"description must not contain tabs, got: {tasks[0]['description']!r}"
    )
    assert tasks[0]["description"] == "task with tab"


def test_add_description_with_multiple_tabs_collapsed():
    """Multiple consecutive tabs must be collapsed to a single space (#728)."""
    res = fn(action="add", description="a\t\tb")
    assert "Added task #1" in res, f"Expected add success, got: {res!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["description"] == "a b", (
        f"Expected 'a b', got: {tasks[0]['description']!r}"
    )


def test_add_description_with_mixed_tab_and_newline_collapsed():
    """Mixed tabs and newlines in description must all be collapsed (#728)."""
    res = fn(action="add", description="line1\n\tline2")
    assert "Added task #1" in res, f"Expected add success, got: {res!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    desc = tasks[0]["description"]
    assert "\t" not in desc and "\n" not in desc, (
        f"description must contain no tabs or newlines, got: {desc!r}"
    )
    assert desc == "line1 line2", f"Expected 'line1 line2', got: {desc!r}"


def test_update_note_with_tab_collapsed():
    """note passed to update with tab characters must be collapsed (#728)."""
    fn(action="add", description="Task to annotate")
    res = fn(action="update", task_id=1, description="note\twith\ttabs")
    assert "Updated task #1" in res, f"Expected update success, got: {res!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    note = tasks[0].get("note", "")
    assert "\t" not in note, f"note must not contain tabs, got: {note!r}"
    assert note == "note with tabs", f"Expected 'note with tabs', got: {note!r}"


def test_done_note_with_tab_collapsed():
    """note passed to done with tab characters must be collapsed (#728)."""
    fn(action="add", description="Task to finish")
    res = fn(action="done", task_id=1, description="finished\twith\tnote")
    assert "Completed task #1" in res, f"Expected completion, got: {res!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    note = tasks[0].get("note", "")
    assert "\t" not in note, f"note must not contain tabs, got: {note!r}"
    assert note == "finished with note", f"Expected 'finished with note', got: {note!r}"


def test_list_output_not_broken_by_tab_in_description():
    """list output must keep one task per line even when description had tabs (#728)."""
    fn(action="add", description="tab\ttest\ttask")
    result = fn(action="list")
    task_lines = [line for line in result.splitlines() if "#1" in line]
    assert len(task_lines) == 1, (
        f"Expected exactly one line mentioning #1, got: {result!r}"
    )
    assert "[ ] #1 (open): tab test task" in result


def test_add_tab_only_description_rejected():
    """description='\\t' (tab only) must return Error, not create a blank task (#728)."""
    res = fn(action="add", description="\t")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "description required" in res


def test_add_tab_and_space_only_description_rejected():
    """description='  \\t  ' (whitespace + tab only) must return Error (#728)."""
    res = fn(action="add", description="  \t  \t  ")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "description required" in res


def test_description_with_space_around_tab_collapsed_cleanly():
    """Spaces surrounding a tab (e.g. 'a \\t b') must collapse to single space (#728)."""
    res = fn(action="add", description="word1 \t word2")
    assert "Added task #1" in res, f"Expected add success, got: {res!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    desc = tasks[0]["description"]
    assert desc == "word1 word2", f"Expected 'word1 word2', got: {desc!r}"


# ── Issue #732: list action must honour the status filter ────────────────────

def test_list_status_filter_no_match_returns_clear_message():
    """list with a status filter that matches no tasks must return a clear message, not all tasks (#732)."""
    fn(action="add", description="open task")
    result = fn(action="list", status="blocked")
    assert "open task" not in result, (
        f"Expected no tasks in output, got: {result!r}"
    )
    assert "blocked" in result, f"Expected 'blocked' in message, got: {result!r}"


def test_list_status_filter_returns_only_matching_tasks():
    """list with status='blocked' must return only blocked tasks, not open or done ones (#732)."""
    fn(action="add", description="open task")
    fn(action="add", description="blocked task")
    fn(action="update", task_id=2, status="blocked")
    fn(action="add", description="done task")
    fn(action="done", task_id=3)

    result = fn(action="list", status="blocked")
    assert "blocked task" in result, f"Expected 'blocked task' in output, got: {result!r}"
    assert "open task" not in result, f"open task must not appear in filtered output: {result!r}"
    assert "done task" not in result, f"done task must not appear in filtered output: {result!r}"


def test_list_status_filter_case_insensitive():
    """list with status='OPEN' or 'Open' must match tasks with status 'open' (#732)."""
    fn(action="add", description="open task")

    result_upper = fn(action="list", status="OPEN")
    assert "open task" in result_upper, (
        f"UPPERCASE filter must match open tasks, got: {result_upper!r}"
    )

    result_mixed = fn(action="list", status="Open")
    assert "open task" in result_mixed, (
        f"Mixed-case filter must match open tasks, got: {result_mixed!r}"
    )


def test_list_no_status_filter_returns_all_tasks():
    """list without a status filter must still return all tasks (existing behaviour, #732)."""
    fn(action="add", description="open task")
    fn(action="add", description="another open")
    fn(action="update", task_id=2, status="in_progress")

    result = fn(action="list")
    assert "open task" in result
    assert "another open" in result


def test_list_status_filter_in_progress():
    """list with status='in_progress' must return only in_progress tasks (#732)."""
    fn(action="add", description="task a")
    fn(action="add", description="task b")
    fn(action="update", task_id=1, status="in_progress")

    result = fn(action="list", status="in_progress")
    assert "task a" in result, f"Expected 'task a' in output, got: {result!r}"
    assert "task b" not in result, f"task b must not appear in filtered output: {result!r}"


def test_list_status_filter_summary_counts_reflect_full_list():
    """Summary counts in filtered list must reflect the full task list, not just filtered (#732)."""
    fn(action="add", description="open task")
    fn(action="add", description="blocked task")
    fn(action="update", task_id=2, status="blocked")

    result = fn(action="list", status="blocked")
    # Total: 1 open, 1 blocked, 0 done — summary reflects the full list, not just the filter.
    # (#748: blocked is no longer lumped into the 'open' count)
    assert "1 open" in result, (
        f"Summary counts must be for full list, not just filter match. Got: {result!r}"
    )
    assert "1 blocked" in result, (
        f"Summary must show separate blocked count. Got: {result!r}"
    )


# ── Issue #736: float task_id with fractional part must be rejected ───────────

def test_done_fractional_float_task_id_rejected():
    """task_id=1.5 must return Error rather than silently truncating to 1 (#736)."""
    fn(action="add", description="do not complete via float")
    res = fn(action="done", task_id=1.5)
    assert res.startswith("Error:"), (
        f"Expected Error for fractional float task_id=1.5, got: {res!r}"
    )
    assert "1.5" in res, f"Error message should mention the bad value 1.5, got: {res!r}"


def test_update_fractional_float_task_id_rejected():
    """task_id=2.9 must return Error for 'update' — not truncate to 2 (#736)."""
    fn(action="add", description="update via float test")
    res = fn(action="update", task_id=2.9, status="in_progress")
    assert res.startswith("Error:"), (
        f"Expected Error for fractional float task_id=2.9, got: {res!r}"
    )
    assert "2.9" in res, f"Error message should mention the bad value 2.9, got: {res!r}"


def test_drop_fractional_float_task_id_rejected():
    """task_id=1.1 must return Error for 'drop' (#736)."""
    fn(action="add", description="drop via float test")
    res = fn(action="drop", task_id=1.1)
    assert res.startswith("Error:"), (
        f"Expected Error for fractional float task_id=1.1, got: {res!r}"
    )
    assert "1.1" in res, f"Error message should mention the bad value 1.1, got: {res!r}"


def test_whole_number_float_task_id_still_coerced():
    """task_id=1.0 (whole-number float) must still be accepted and coerced to 1 (#736)."""
    fn(action="add", description="whole float coerce test")
    res = fn(action="done", task_id=1.0)
    assert "Completed task #1" in res, (
        f"Whole-number float task_id=1.0 should coerce to 1, got: {res!r}"
    )


def test_fractional_float_does_not_modify_wrong_task():
    """task_id=1.9 must NOT complete task #1 — the task must remain open (#736)."""
    fn(action="add", description="should stay open")
    res = fn(action="done", task_id=1.9)
    # Must be an error
    assert res.startswith("Error:"), (
        f"Expected Error for task_id=1.9, got: {res!r}"
    )
    # Task #1 must still be open
    listing = fn(action="list")
    assert "should stay open" in listing, (
        f"Task must remain open after rejected fractional float. Listing: {listing!r}"
    )
    assert "[x] #1" not in listing, (
        f"Task #1 must not be marked done. Listing: {listing!r}"
    )


# ── Issue #738: list must treat 'completed' as a terminal status ──────────────

def test_list_completed_task_shows_checked_marker():
    """list must display [x] for a task with status='completed' (#738)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [{"id": 1, "description": "legacy task", "status": "completed", "created": "2024-01-01T00:00:00"}]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="list")
    assert "[x] #1" in result, (
        f"Expected [x] marker for completed task, got: {result!r}"
    )
    assert "[ ] #1" not in result, (
        f"completed task must not show as unchecked: {result!r}"
    )


def test_list_completed_task_counted_as_done_not_open():
    """list summary must count 'completed' tasks as done, not open (#738)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "legacy task", "status": "completed", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "open task", "status": "open", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="list")
    assert "1 open, 1 done" in result, (
        f"Expected '1 open, 1 done' (completed counts as done), got: {result!r}"
    )


def test_list_completed_and_done_both_counted_as_done():
    """list summary must count both 'done' and 'completed' tasks in done total (#738)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "completed task", "status": "completed", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "done task", "status": "done", "created": "2024-01-01T00:00:00"},
        {"id": 3, "description": "open task", "status": "open", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="list")
    assert "1 open, 2 done" in result, (
        f"Expected '1 open, 2 done' (both done and completed count as done), got: {result!r}"
    )
    assert "[x] #1" in result, "completed task must show [x]"
    assert "[x] #2" in result, "done task must show [x]"
    assert "[ ] #3" in result, "open task must show [ ]"


# ── Issue #740: done/update 'Open tasks:' hint must exclude 'completed' tasks ──

def test_done_missing_task_id_hint_excludes_completed_tasks():
    """done without task_id must not list 'completed' tasks in 'Open tasks:' hint (#740).

    Two open tasks are required so that auto-resolution does not kick in (which
    would silently complete the unique open task without showing the hint).
    """
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "legacy completed", "status": "completed", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "still open A", "status": "open", "created": "2024-01-01T00:00:00"},
        {"id": 3, "description": "still open B", "status": "open", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="done", task_id=0)
    assert "task_id required" in result, f"Expected task_id-required error, got: {result!r}"
    assert "#1" not in result, (
        f"'Open tasks:' hint must not include completed task #1, got: {result!r}"
    )
    assert "#2" in result, (
        f"'Open tasks:' hint must include open task #2, got: {result!r}"
    )
    assert "#3" in result, (
        f"'Open tasks:' hint must include open task #3, got: {result!r}"
    )


def test_done_missing_task_id_hint_empty_when_only_completed_tasks():
    """done without task_id shows '(none)' when only 'completed' legacy tasks exist (#740)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "legacy completed", "status": "completed", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="done", task_id=0)
    assert "(none)" in result, (
        f"Expected '(none)' when no open tasks remain, got: {result!r}"
    )
    assert "#1" not in result, (
        f"completed task #1 must not appear in hint, got: {result!r}"
    )


def test_update_missing_task_id_hint_excludes_completed_tasks():
    """update without task_id must not list 'completed' tasks in 'Open tasks:' hint (#740).

    Two open tasks are required so that auto-resolution does not kick in (which
    would silently update the unique open task without showing the hint).
    """
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "legacy completed", "status": "completed", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "still open A", "status": "open", "created": "2024-01-01T00:00:00"},
        {"id": 3, "description": "still open B", "status": "open", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="update", task_id=0, status="in_progress")
    assert "task_id required" in result, f"Expected task_id-required error, got: {result!r}"
    assert "#1" not in result, (
        f"'Open tasks:' hint must not include completed task #1, got: {result!r}"
    )
    assert "#2" in result, (
        f"'Open tasks:' hint must include open task #2, got: {result!r}"
    )
    assert "#3" in result, (
        f"'Open tasks:' hint must include open task #3, got: {result!r}"
    )


def test_update_missing_task_id_hint_empty_when_only_completed_tasks():
    """update without task_id shows '(none)' when only 'completed' legacy tasks exist (#740)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "legacy completed", "status": "completed", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="update", task_id=0, status="in_progress")
    assert "(none)" in result, (
        f"Expected '(none)' when no open tasks remain, got: {result!r}"
    )
    assert "#1" not in result, (
        f"completed task #1 must not appear in hint, got: {result!r}"
    )


# ── Issue #748: list summary must not conflate in_progress/blocked/deferred with open ──

def test_list_summary_counts_only_open_status_as_open():
    """Summary 'open' count must only include tasks with status='open', not in_progress/blocked/deferred (#748)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "open task", "status": "open", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "in_progress task", "status": "in_progress", "created": "2024-01-01T00:00:00"},
        {"id": 3, "description": "blocked task", "status": "blocked", "created": "2024-01-01T00:00:00"},
        {"id": 4, "description": "deferred task", "status": "deferred", "created": "2024-01-01T00:00:00"},
        {"id": 5, "description": "done task", "status": "done", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="list")
    # open count must be exactly 1 (only the task with status="open")
    assert "1 open" in result, (
        f"Expected '1 open' (only status=open counts), got: {result!r}"
    )
    # in_progress, blocked, deferred each appear in summary
    assert "1 in_progress" in result, (
        f"Expected '1 in_progress' in summary, got: {result!r}"
    )
    assert "1 blocked" in result, (
        f"Expected '1 blocked' in summary, got: {result!r}"
    )
    assert "1 deferred" in result, (
        f"Expected '1 deferred' in summary, got: {result!r}"
    )
    assert "1 done" in result, (
        f"Expected '1 done' in summary, got: {result!r}"
    )
    # The old wrong count ("5 open") must not appear
    assert "5 open" not in result, (
        f"Must not report non-done tasks as '5 open', got: {result!r}"
    )
    assert "4 open" not in result, (
        f"Must not count in_progress/blocked/deferred as open, got: {result!r}"
    )


def test_list_summary_omits_zero_active_statuses():
    """Summary must not include in_progress/blocked/deferred parts when their count is 0 (#748)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "task one", "status": "open", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "task two", "status": "done", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="list")
    # Plain summary: no in_progress/blocked/deferred parts present
    assert "1 open, 1 done" in result, (
        f"Expected plain '1 open, 1 done' when no active statuses, got: {result!r}"
    )
    assert "in_progress" not in result, (
        f"Must not mention in_progress when count is 0, got: {result!r}"
    )
    assert "blocked" not in result, (
        f"Must not mention blocked when count is 0, got: {result!r}"
    )
    assert "deferred" not in result, (
        f"Must not mention deferred when count is 0, got: {result!r}"
    )


def test_list_summary_active_statuses_partial_mix():
    """Summary includes only the active status buckets that are non-zero (#748)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "task a", "status": "open", "created": "2024-01-01T00:00:00"},
        {"id": 2, "description": "task b", "status": "open", "created": "2024-01-01T00:00:00"},
        {"id": 3, "description": "task c", "status": "blocked", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    result = fn(action="list")
    assert "2 open" in result, f"Expected '2 open', got: {result!r}"
    assert "1 blocked" in result, f"Expected '1 blocked', got: {result!r}"
    assert "0 done" in result, f"Expected '0 done', got: {result!r}"
    assert "in_progress" not in result, f"Must not mention in_progress when count is 0, got: {result!r}"
    assert "deferred" not in result, f"Must not mention deferred when count is 0, got: {result!r}"
    assert "3 open" not in result, (
        f"Must not lump blocked into open count, got: {result!r}"
    )


# ── Null-byte validation tests (#762) ──────────────────────────────────────────

def test_add_null_byte_in_description_returns_error():
    """Null byte in description must be rejected, not silently stored. (#762)"""
    result = fn(action="add", description="hello\x00world")
    assert "null byte" in result, f"Expected null byte error, got: {result!r}"
    assert "Error" in result

def test_add_null_byte_only_description_returns_error():
    """A description that is just \\x00 must also be rejected."""
    result = fn(action="add", description="\x00")
    assert "null byte" in result, f"Expected null byte error, got: {result!r}"

def test_done_null_byte_in_note_returns_error():
    """Null byte in the note (description arg to done) must be rejected. (#762)"""
    fn(action="add", description="a real task")
    result = fn(action="done", task_id=1, description="note\x00with null")
    assert "null byte" in result, f"Expected null byte error, got: {result!r}"

def test_update_null_byte_in_note_returns_error():
    """Null byte in note (description arg to update) must be rejected. (#762)"""
    fn(action="add", description="task to update")
    result = fn(action="update", task_id=1, description="note\x00bad")
    assert "null byte" in result, f"Expected null byte error, got: {result!r}"

def test_null_byte_rejected_before_storage():
    """After a rejected null-byte add, the tasks file must remain clean. (#762)"""
    fn(action="add", description="hello\x00world")
    # No tasks should have been written
    p = Path(_TASKS_FILE)
    if p.exists():
        tasks = json.loads(p.read_text())
        for t in tasks:
            assert "\x00" not in t.get("description", ""), \
                f"Null byte found in stored task: {t!r}"

def test_valid_description_still_works_after_null_check():
    """The null-byte guard must not break normal adds. (#762)"""
    result = fn(action="add", description="a normal task description")
    assert "Added task" in result, f"Expected success, got: {result!r}"


# ── bool task_id regression tests (#769) ────────────────────────────────────

def test_done_task_id_true_returns_error():
    """task_id=True must be rejected — bool True==1 in Python and would silently
    operate on task #1 without any indication the type was wrong. (#769)"""
    fn(action="add", description="test task")
    result = fn(action="done", task_id=True)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention 'bool', got: {result!r}"
    # Verify task was NOT completed (no silent mutation)
    listing = fn(action="list")
    assert "open" in listing, f"Task should still be open, got: {listing!r}"


def test_done_task_id_false_returns_error():
    """task_id=False must be rejected — bool False==0 in Python and would be treated
    as 'no task_id specified', triggering auto-resolve logic. (#769)"""
    fn(action="add", description="test task")
    result = fn(action="done", task_id=False)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention 'bool', got: {result!r}"


def test_update_task_id_true_returns_error():
    """task_id=True must be rejected for update action too. (#769)"""
    fn(action="add", description="test task")
    result = fn(action="update", task_id=True, status="in_progress")
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention 'bool', got: {result!r}"


def test_drop_task_id_true_returns_error():
    """task_id=True must be rejected for drop action. (#769)"""
    fn(action="add", description="test task")
    result = fn(action="drop", task_id=True)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "bool" in result, f"Error must mention 'bool', got: {result!r}"
    # Verify task was NOT dropped
    listing = fn(action="list")
    assert "#1" in listing, f"Task should still exist, got: {listing!r}"


def test_task_id_int_still_works_after_bool_check():
    """Plain integer task_id must not be broken by the bool guard. (#769)"""
    fn(action="add", description="task for int id test")
    result = fn(action="done", task_id=1)
    assert "Completed task #1" in result, f"Expected success, got: {result!r}"


# ── edge case: empty/whitespace description, unknown action (#770) ─────────────

def test_add_empty_description_returns_error():
    """add with an empty string description must return a clear error, not add a blank task."""
    result = fn(action="add", description="")
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "description required" in result.lower() or "description" in result


def test_add_whitespace_only_description_returns_error():
    """add with a whitespace-only description (stripped to empty) must return a clear error."""
    result = fn(action="add", description="   ")
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "description" in result.lower()


def test_unknown_action_returns_clear_error():
    """An unrecognised action (e.g. 'hint') must return a clear error listing valid actions."""
    result = fn(action="hint", task_id=0)
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "unknown action" in result.lower()
    # Must list the valid action names so the caller can self-correct
    for valid in ("add", "done", "update", "drop", "list"):
        assert valid in result, f"Valid action '{valid}' missing from error message: {result!r}"


def test_unknown_action_with_no_tasks_returns_error():
    """Unknown action must return an error even when no tasks exist."""
    result = fn(action="bogus")
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "unknown action" in result.lower()


# ── Issue #772: corrupted JSON — wrong structure (not a list) ─────────────────

def test_json_dict_structure_returns_corrupted_error():
    """tasks.json containing a dict (not a list) must return a clear corruption error (#772)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    res = fn(action="list")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "corrupted" in res.lower(), f"Expected 'corrupted' mention, got: {res!r}"


def test_json_null_structure_returns_corrupted_error():
    """tasks.json containing JSON null must return a clear corruption error, not 'No tasks.' (#772)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("null", encoding="utf-8")
    res = fn(action="list")
    assert res.startswith("Error:"), f"Expected Error for null JSON, got: {res!r}"
    assert "corrupted" in res.lower(), f"Expected 'corrupted' mention, got: {res!r}"


def test_json_list_of_non_dicts_returns_corrupted_error():
    """tasks.json containing a list of non-dict elements must return a corruption error (#772)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(["string", 42]), encoding="utf-8")
    res = fn(action="list")
    assert res.startswith("Error:"), f"Expected Error for list of non-dicts, got: {res!r}"
    assert "corrupted" in res.lower(), f"Expected 'corrupted' mention, got: {res!r}"


def test_json_wrong_structure_add_returns_error():
    """add must not overwrite a file with wrong JSON structure (dict instead of list) (#772)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"not": "a list"})
    p.write_text(original, encoding="utf-8")
    res = fn(action="add", description="new task")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    # File must not be overwritten
    assert p.read_text(encoding="utf-8") == original


def test_json_wrong_structure_all_actions_return_error():
    """Every action must return Error when the file has wrong JSON structure (#772)."""
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tasks": []}), encoding="utf-8")
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


# ── Issue #773: list unknown status filter must return an error, not silently empty ──

def test_list_unknown_status_filter_returns_error():
    """list with a completely unknown status filter must return Error, not a silent empty message (#773)."""
    fn(action="add", description="some task")
    res = fn(action="list", status="nonexistent_status")
    assert res.startswith("Error:"), f"Expected Error for unknown status filter, got: {res!r}"
    assert "nonexistent_status" in res, f"Error must mention the bad value, got: {res!r}"


def test_list_unknown_status_filter_mentions_valid_values():
    """Error for unknown status filter must list the valid status values (#773)."""
    res = fn(action="list", status="bogus_status")
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    for valid in ("open", "in_progress", "blocked", "deferred", "done", "completed"):
        assert valid in res, f"Valid status '{valid}' missing from error, got: {res!r}"


def test_list_valid_status_filter_still_works_after_validation():
    """Adding unknown-status validation must not break valid status filters (#773)."""
    fn(action="add", description="open task")
    fn(action="add", description="blocked task")
    fn(action="update", task_id=2, status="blocked")
    res = fn(action="list", status="blocked")
    assert "blocked task" in res, f"Expected 'blocked task', got: {res!r}"
    assert res.startswith("Error:") is False, f"Must not error for valid status filter: {res!r}"


def test_list_no_status_filter_unaffected_by_validation():
    """list without status filter must still return all tasks after the validation is added (#773)."""
    fn(action="add", description="task a")
    fn(action="add", description="task b")
    res = fn(action="list")
    assert "task a" in res
    assert "task b" in res


# ── drop must refuse done/completed tasks to preserve history ─────────────────

def test_drop_on_done_task_returns_error():
    """drop on a done task must return Error, not silently erase completed history."""
    fn(action="add", description="task to protect")
    fn(action="done", task_id=1)
    res = fn(action="drop", task_id=1)
    assert res.startswith("Error:"), f"Expected Error, got: {res!r}"
    assert "already done" in res, f"Error must mention 'already done', got: {res!r}"


def test_drop_on_done_task_does_not_remove_record():
    """A rejected drop on a done task must leave the task record intact."""
    fn(action="add", description="finished task")
    fn(action="done", task_id=1)
    fn(action="drop", task_id=1)  # should be rejected
    # Task must still appear in the list
    result = fn(action="list")
    assert "finished task" in result, (
        f"Task record must be preserved after rejected drop, got: {result!r}"
    )


def test_drop_on_completed_status_task_returns_error():
    """drop on a legacy 'completed'-status task must also return Error (covers both terminal statuses)."""
    Path(_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": 1, "description": "legacy task", "status": "completed", "created": "2024-01-01T00:00:00"},
    ]
    Path(_TASKS_FILE).write_text(json.dumps(tasks))
    res = fn(action="drop", task_id=1)
    assert res.startswith("Error:"), f"Expected Error for completed task, got: {res!r}"
    assert "already done" in res, f"Error must mention 'already done', got: {res!r}"


def test_drop_on_open_task_still_works():
    """Regression: drop on an open (non-done) task must still succeed after the guard."""
    fn(action="add", description="open task to drop")
    res = fn(action="drop", task_id=1)
    assert "Dropped task #1" in res, f"Expected drop success, got: {res!r}"
    result = fn(action="list")
    assert "open task to drop" not in result, "Dropped task must be gone"


# ── Issue #776: auto-resolve ambiguity must return a helpful error ────────────

def test_done_auto_resolve_ambiguous_match_returns_error():
    """done with description matching multiple tasks must return an error listing
    the matches, not silently pick one or return a generic 'task_id required' message. (#776)"""
    fn(action="add", description="fix login bug")
    fn(action="add", description="fix signup bug")
    fn(action="add", description="fix logout bug")
    result = fn(action="done", task_id=0, description="fix")
    assert result.startswith("Error:"), f"Expected Error, got: {result!r}"
    # Must mention the ambiguity — how many tasks matched
    assert "3 tasks match" in result or "3 task" in result, (
        f"Error must mention how many tasks matched, got: {result!r}"
    )
    # Must list the matching task IDs so the caller can resolve manually
    assert "#1" in result, f"Match list must include #1, got: {result!r}"
    assert "#2" in result, f"Match list must include #2, got: {result!r}"
    assert "#3" in result, f"Match list must include #3, got: {result!r}"
    # Must mention 'task_id' so the caller knows how to fix it
    assert "task_id" in result, f"Error must mention task_id, got: {result!r}"


def test_update_auto_resolve_ambiguous_match_returns_error():
    """update with description matching multiple tasks must return a clear ambiguity error. (#776)"""
    fn(action="add", description="fix login bug")
    fn(action="add", description="fix signup bug")
    result = fn(action="update", task_id=0, description="fix", status="in_progress")
    assert result.startswith("Error:"), f"Expected Error, got: {result!r}"
    assert "2 tasks match" in result or "2 task" in result, (
        f"Error must mention 2 matching tasks, got: {result!r}"
    )
    assert "#1" in result and "#2" in result, f"Both task IDs must appear in error, got: {result!r}"


def test_drop_auto_resolve_ambiguous_match_returns_error():
    """drop with description matching multiple tasks must return a clear ambiguity error. (#776)"""
    fn(action="add", description="cleanup old logs")
    fn(action="add", description="cleanup old caches")
    result = fn(action="drop", task_id=0, description="cleanup")
    assert result.startswith("Error:"), f"Expected Error, got: {result!r}"
    assert "2 tasks match" in result or "2 task" in result, (
        f"Error must mention 2 matching tasks, got: {result!r}"
    )
    # Neither task must have been dropped
    listing = fn(action="list")
    assert "cleanup old logs" in listing, "task #1 must still exist after ambiguous drop"
    assert "cleanup old caches" in listing, "task #2 must still exist after ambiguous drop"


def test_auto_resolve_unique_match_still_works_after_ambiguity_fix():
    """Auto-resolve on a unique description match must still succeed after the ambiguity guard. (#776)"""
    fn(action="add", description="fix login bug")
    fn(action="add", description="update docs")
    # 'login' matches only task #1 — should auto-resolve successfully
    result = fn(action="done", task_id=0, description="login")
    assert "Completed task #1" in result, f"Expected completion, got: {result!r}"


def test_auto_resolve_no_match_falls_through_to_task_id_required():
    """When description matches no open tasks (and there are multiple open tasks),
    the existing 'task_id required' error must be returned (not a match-count error). (#776)"""
    fn(action="add", description="fix login bug")
    fn(action="add", description="update docs")
    # Description doesn't match either task
    result = fn(action="done", task_id=0, description="deploy hotfix")
    # Falls through to generic error — not an ambiguity error, just missing task_id
    assert "Error:" in result, f"Expected an Error, got: {result!r}"
    # Neither task must have been completed
    listing = fn(action="list")
    assert "fix login bug" in listing
    assert "update docs" in listing


# ── Issue #777: add action must reject non-open initial status values ──────────

def test_add_with_status_blocked_returns_error():
    """add with status='blocked' must return an error — new tasks always start as 'open'. (#777)"""
    result = fn(action="add", description="test task", status="blocked")
    assert result.startswith("Error:"), f"Expected Error, got: {result!r}"
    assert "open" in result, f"Error must mention 'open' as the required initial status, got: {result!r}"


def test_add_with_status_in_progress_returns_error():
    """add with status='in_progress' must return an error. (#777)"""
    result = fn(action="add", description="task x", status="in_progress")
    assert result.startswith("Error:"), f"Expected Error, got: {result!r}"
    assert "open" in result


def test_add_with_status_deferred_returns_error():
    """add with status='deferred' must return an error. (#777)"""
    result = fn(action="add", description="task x", status="deferred")
    assert result.startswith("Error:"), f"Expected Error, got: {result!r}"
    assert "open" in result


def test_add_with_invalid_status_returns_error():
    """add with an unrecognised status must return an error, not silently ignore it. (#777)"""
    result = fn(action="add", description="task x", status="invalid_status")
    assert result.startswith("Error:"), f"Expected Error, got: {result!r}"
    assert "invalid_status" in result or "invalid" in result.lower()


def test_add_with_status_open_succeeds():
    """add with status='open' must succeed (it is the default initial status). (#777)"""
    result = fn(action="add", description="normal task", status="open")
    assert "Added task #1" in result, f"Expected success for status='open', got: {result!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "open"


def test_add_without_status_still_creates_open_task():
    """add without status must still work and default to 'open'. (#777)"""
    result = fn(action="add", description="plain task")
    assert "Added task #1" in result, f"Expected success, got: {result!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "open"


def test_add_status_rejected_task_not_stored():
    """When add is rejected due to invalid status, no task must be written to disk. (#777)"""
    result = fn(action="add", description="rejected task", status="blocked")
    assert result.startswith("Error:")
    # Either no file exists, or it contains zero tasks
    p = Path(_TASKS_FILE)
    if p.exists():
        tasks = json.loads(p.read_text())
        assert len(tasks) == 0, f"No task should be stored after rejection, got: {tasks!r}"
    else:
        pass  # File not created — also correct


# ── Issue #778: update guard regression tests ─────────────────────────────────

def test_update_empty_description_with_no_status_returns_error():
    """update with empty description and no status must return Error, not silently no-op (#778).

    The top-of-fn strip() reduces '' to '', and _effective_description='' triggers
    the 'requires at least one of: status or description' guard.
    """
    fn(action="add", description="task to guard")
    result = fn(action="update", task_id=1, description="")
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "status" in result or "description" in result, (
        f"Error must mention what's missing, got: {result!r}"
    )
    # Task must not be modified
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "open"
    assert "note" not in tasks[0]


def test_update_whitespace_only_description_with_no_status_returns_error():
    """update with whitespace-only description (stripped to empty) must return Error (#778).

    '   ' strips to '' which is treated as absent — same guard triggers.
    """
    fn(action="add", description="task to guard")
    result = fn(action="update", task_id=1, description="   ")
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"
    assert "status" in result or "description" in result, (
        f"Error must mention what's missing, got: {result!r}"
    )
    # Task must not be modified
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "open"
    assert "note" not in tasks[0]


def test_update_tab_only_description_with_no_status_returns_error():
    """update with tab-only description must also be rejected (#778)."""
    fn(action="add", description="task for tab test")
    result = fn(action="update", task_id=1, description="\t\t")
    assert result.startswith("Error:"), f"Expected Error:, got: {result!r}"


def test_update_whitespace_description_with_status_succeeds():
    """update with whitespace-only description but valid status must succeed (#778).

    The whitespace description is treated as absent; status alone is enough.
    """
    fn(action="add", description="task to progress")
    result = fn(action="update", task_id=1, description="   ", status="in_progress")
    assert "Updated task #1" in result, f"Expected success, got: {result!r}"
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    assert tasks[0]["status"] == "in_progress"
    # No note must be stored — the whitespace description was discarded
    assert "note" not in tasks[0], (
        f"Whitespace-only description must not be stored as a note, got: {tasks[0]!r}"
    )


# ── Issue #778: task ID stability after drop ──────────────────────────────────

def test_task_ids_stable_after_drop():
    """Task IDs must remain stable after a drop — dropped gaps are not backfilled (#778).

    Dropping #2 from [1,2,3] must leave IDs [1,3], not renumber to [1,2].
    """
    fn(action="add", description="task one")
    fn(action="add", description="task two")
    fn(action="add", description="task three")
    fn(action="drop", task_id=2)
    result = fn(action="list")
    assert "#1" in result, f"Task #1 must still exist, got: {result!r}"
    assert "#3" in result, f"Task #3 must still exist, got: {result!r}"
    assert "#2" not in result, f"Dropped task #2 must not appear, got: {result!r}"
    # Verify the IDs are stored correctly
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    ids = [t["id"] for t in tasks]
    assert ids == [1, 3], f"Expected IDs [1, 3] after dropping #2, got: {ids!r}"


def test_done_on_original_id_after_drop():
    """done on a task whose ID was NOT dropped must work correctly after a drop (#778).

    After dropping #2 from [1,2,3], completing #3 by its original ID must succeed.
    """
    fn(action="add", description="task one")
    fn(action="add", description="task two")
    fn(action="add", description="task three")
    fn(action="drop", task_id=2)
    result = fn(action="done", task_id=3)
    assert "Completed task #3" in result, (
        f"Expected done on original #3 to succeed after dropping #2, got: {result!r}"
    )
    tasks = json.loads(Path(_TASKS_FILE).read_text())
    task3 = next(t for t in tasks if t["id"] == 3)
    assert task3["status"] == "done", f"Task #3 must be done, got: {task3!r}"


def test_new_task_after_drop_gets_next_max_id():
    """A new task added after a drop must get the next ID above the current max, not reuse a gap (#778).

    After [1,2,3] drop #2 → [1,3], the next add must assign ID 4, not reuse 2.
    """
    fn(action="add", description="task one")
    fn(action="add", description="task two")
    fn(action="add", description="task three")
    fn(action="drop", task_id=2)
    result = fn(action="add", description="task four")
    assert "Added task #4" in result, (
        f"New task after drop must get ID 4 (not reuse 2), got: {result!r}"
    )


# ── Permission-denied regression tests (#781) ────────────────────────────────

def test_add_to_read_only_parent_dir_returns_clear_error():
    """Adding a task when the tasks-file parent directory is read-only must return
    a clear 'permission denied' error string, not raise an uncaught PermissionError."""
    orig = _tt_mod._TASKS_FILE
    tmpdir = tempfile.mkdtemp()
    try:
        os.chmod(tmpdir, 0o444)  # read-only directory — cannot create files inside
        _tt_mod._TASKS_FILE = os.path.join(tmpdir, "tasks.json")
        result = _tt_mod.fn(action="add", description="should fail")
        assert isinstance(result, str), "fn() must always return a string"
        assert "Error" in result, f"Expected error string, got: {result!r}"
        assert "permission denied" in result.lower() or "Permission denied" in result, (
            f"Expected 'permission denied' in message, got: {result!r}"
        )
        assert "[Errno 13]" not in result, (
            f"Must not expose raw errno format, got: {result!r}"
        )
    finally:
        os.chmod(tmpdir, 0o755)
        shutil.rmtree(tmpdir)
        _tt_mod._TASKS_FILE = orig


def test_load_tasks_from_restricted_dir_returns_clear_error():
    """_load_tasks() on a completely inaccessible directory must return a _Corrupted
    sentinel with a clear message, not raise an uncaught PermissionError (#781)."""
    orig = _tt_mod._TASKS_FILE
    tmpdir = tempfile.mkdtemp()
    try:
        os.chmod(tmpdir, 0o000)  # completely inaccessible
        _tt_mod._TASKS_FILE = os.path.join(tmpdir, "subdir", "tasks.json")
        result = _tt_mod.fn(action="add", description="should fail")
        assert isinstance(result, str), "fn() must always return a string"
        assert "Error" in result, f"Expected error string, got: {result!r}"
    finally:
        os.chmod(tmpdir, 0o755)
        shutil.rmtree(tmpdir)
        _tt_mod._TASKS_FILE = orig


# ── Regression: add with explicit task_id (#fix-misc-edges) ──

def test_add_with_explicit_task_id_returns_error():
    """task_id is not valid for 'add' — must return an error, not silently ignore."""
    result = fn(action="add", description="some task", task_id=99)
    assert "Error" in result
    assert "task_id=99" in result
    assert "not valid for 'add'" in result


def test_add_without_task_id_still_works():
    """Normal add (no task_id) must still work after the task_id guard."""
    result = fn(action="add", description="normal task")
    assert "Added task #1" in result


# ── Regression: list with limit parameter (#fix-misc-edges) ──

def test_list_with_limit_returns_subset():
    """list with limit=3 must return at most 3 task lines."""
    for i in range(10):
        fn(action="add", description=f"task {i+1}")
    result = fn(action="list", limit=3)
    # Should show 3 tasks and a "showing N of M" note
    task_lines = [l for l in result.splitlines() if l.startswith("[")]
    assert len(task_lines) == 3, f"Expected 3 task lines, got {len(task_lines)}: {result!r}"
    assert "showing 3 of 10" in result


def test_list_with_limit_zero_returns_all():
    """list with limit=0 (default) must return all tasks."""
    for i in range(5):
        fn(action="add", description=f"task {i+1}")
    result = fn(action="list", limit=0)
    task_lines = [l for l in result.splitlines() if l.startswith("[")]
    assert len(task_lines) == 5
    assert "showing" not in result


def test_list_with_limit_larger_than_total_returns_all():
    """list with limit > total tasks must return all tasks without truncation note."""
    for i in range(3):
        fn(action="add", description=f"task {i+1}")
    result = fn(action="list", limit=10)
    task_lines = [l for l in result.splitlines() if l.startswith("[")]
    assert len(task_lines) == 3
    assert "showing" not in result


def test_list_limit_negative_returns_error():
    """A negative limit must return an error."""
    fn(action="add", description="task")
    result = fn(action="list", limit=-1)
    assert "Error" in result
    assert "limit" in result


def test_list_limit_bool_returns_error():
    """limit=True must be rejected (bool is subclass of int but not a valid limit)."""
    fn(action="add", description="task")
    result = fn(action="list", limit=True)
    assert "Error" in result
    assert "bool" in result


# ── limit=<float> edge cases (#784) ──────────────────────────────────────────

def test_list_limit_non_integer_float_returns_error():
    """limit=2.5 (non-integer float) must return an error, not silently truncate to 2 (#784)."""
    for i in range(5):
        fn(action="add", description=f"task {i+1}")
    result = fn(action="list", limit=2.5)
    assert result.startswith("Error:"), f"Expected Error for float limit, got: {result!r}"
    assert "integer" in result.lower(), f"Error must mention 'integer': {result!r}"
    # Must suggest the nearest integers
    assert "2" in result and "3" in result, f"Error must hint at nearest integers: {result!r}"


def test_list_limit_whole_float_is_accepted():
    """limit=2.0 (whole-number float) must be coerced to 2 and work correctly (#784)."""
    for i in range(5):
        fn(action="add", description=f"task {i+1}")
    result = fn(action="list", limit=2.0)
    # Must not be an error
    assert not result.startswith("Error:"), f"Whole-number float limit must not error: {result!r}"
    task_lines = [l for l in result.splitlines() if l.startswith("[")]
    assert len(task_lines) == 2, f"Expected 2 tasks with limit=2.0, got {len(task_lines)}: {result!r}"
