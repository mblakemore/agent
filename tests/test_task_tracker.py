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

def test_json_corruption():
    # Create a corrupted JSON file
    p = Path(_TASKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("NOT JSON", encoding='utf-8')
    
    # _load_tasks should handle this and return []
    res = fn(action="list")
    assert res == "No tasks."
