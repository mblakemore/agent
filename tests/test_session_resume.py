"""Regression tests for session-resume auto-close + summary (AC3/AC4 of #1028).

AC3 — at new-session startup, open tasks without persistent=True are
       transitioned to status='auto_closed'; persistent tasks survive.
AC4 — if any tasks were auto-closed OR any persistent tasks remain open,
       a formatted [Session resume] block is returned for prepending to
       the first user message.
"""
import json
import logging
from pathlib import Path

import pytest


@pytest.fixture
def tasks_cwd(tmp_path, monkeypatch):
    """Run each test in a tmp cwd so .agent/state/tasks.json is isolated."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _seed(tasks):
    p = Path(".agent/state/tasks.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tasks, indent=2))


def _read():
    p = Path(".agent/state/tasks.json")
    if not p.exists():
        return []
    return json.loads(p.read_text())


# ── AC3 — auto_close_ephemeral ─────────────────────────────────────────

def test_auto_close_marks_ephemeral_open_as_auto_closed(tasks_cwd):
    """Open tasks without persistent=True become status='auto_closed'."""
    from tools.task_tracker import auto_close_ephemeral
    _seed([
        {"id": 1, "description": "ephem-one", "status": "open"},
        {"id": 2, "description": "ephem-two", "status": "in_progress"},
    ])

    closed, persistent_open = auto_close_ephemeral()

    assert len(closed) == 2
    assert persistent_open == []
    saved = _read()
    assert all(t["status"] == "auto_closed" for t in saved)
    assert all("closed" in t for t in saved), "closed timestamp must be set"


def test_auto_close_preserves_persistent_tasks(tasks_cwd):
    """persistent=True tasks are NOT auto-closed and appear in persistent_open."""
    from tools.task_tracker import auto_close_ephemeral
    _seed([
        {"id": 1, "description": "ship-order", "status": "open", "persistent": True},
        {"id": 2, "description": "phase-decide", "status": "open"},
    ])

    closed, persistent_open = auto_close_ephemeral()

    assert [t["id"] for t in closed] == [2]
    assert [t["id"] for t in persistent_open] == [1]
    saved_by_id = {t["id"]: t for t in _read()}
    assert saved_by_id[1]["status"] == "open", "persistent task must not be closed"
    assert saved_by_id[2]["status"] == "auto_closed"


def test_auto_close_ignores_already_terminal_tasks(tasks_cwd):
    """Tasks already in done/completed/auto_closed are left untouched."""
    from tools.task_tracker import auto_close_ephemeral
    _seed([
        {"id": 1, "description": "old-done", "status": "done"},
        {"id": 2, "description": "old-completed", "status": "completed"},
        {"id": 3, "description": "old-auto-closed", "status": "auto_closed"},
    ])

    closed, persistent_open = auto_close_ephemeral()

    assert closed == []
    assert persistent_open == []
    statuses = {t["id"]: t["status"] for t in _read()}
    assert statuses == {1: "done", 2: "completed", 3: "auto_closed"}


def test_auto_close_returns_empty_when_no_tasks_file(tasks_cwd):
    """No tasks file ⇒ both lists empty, no exception."""
    from tools.task_tracker import auto_close_ephemeral

    closed, persistent_open = auto_close_ephemeral()

    assert closed == []
    assert persistent_open == []


def test_auto_close_returns_empty_on_corrupted_file(tasks_cwd):
    """Corrupted JSON ⇒ helper returns empties and writes nothing."""
    from tools.task_tracker import auto_close_ephemeral
    p = Path(".agent/state/tasks.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json")

    closed, persistent_open = auto_close_ephemeral()

    assert closed == []
    assert persistent_open == []
    # File untouched
    assert p.read_text() == "{not valid json"


# ── AC4 — _build_session_resume_summary ────────────────────────────────

def test_summary_none_when_no_closed_and_no_persistent_open():
    import agent
    assert agent._build_session_resume_summary([], []) is None


def test_summary_lists_closed_only():
    import agent
    closed = [
        {"id": 12, "description": "Research Bronze Age comps"},
        {"id": 13, "description": "Draft listing cb-2026-0031"},
    ]
    s = agent._build_session_resume_summary(closed, [])
    assert s is not None
    assert s.startswith("[Session resume]")
    assert "2 ephemeral task(s) auto-closed from last session:" in s
    assert "#12 [auto_closed] Research Bronze Age comps" in s
    assert "#13 [auto_closed] Draft listing cb-2026-0031" in s
    assert "persistent" not in s.lower(), "no persistent section when none open"


def test_summary_lists_persistent_only():
    import agent
    persistent = [
        {"id": 9, "description": "Ship cb-2026-0018", "status": "open"},
    ]
    s = agent._build_session_resume_summary([], persistent)
    assert s is not None
    assert "1 persistent task(s) still open:" in s
    assert "#9 [open] Ship cb-2026-0018" in s
    assert "auto-closed" not in s, "no closed section when none closed"


def test_summary_mixed_block_has_both_sections():
    import agent
    closed = [{"id": 14, "description": "CONSOLIDATE"}]
    persistent = [{"id": 9, "description": "Ship cb-2026-0018", "status": "open"}]
    s = agent._build_session_resume_summary(closed, persistent)
    assert s is not None
    assert "1 ephemeral task(s) auto-closed from last session:" in s
    assert "1 persistent task(s) still open:" in s
    # Ordering: closed section appears before persistent section
    assert s.index("auto-closed from last session") < s.index("persistent task(s) still open")


# ── Wire-in — _auto_close_ephemeral_tasks ──────────────────────────────

def test_wire_in_runs_auto_close_and_returns_summary(tasks_cwd):
    """The agent.py wire-in closes ephemeral tasks AND returns the summary."""
    import agent
    _seed([
        {"id": 1, "description": "ephem-A", "status": "open"},
        {"id": 2, "description": "keeper", "status": "in_progress", "persistent": True},
    ])
    log = logging.getLogger("test_wire_in_runs_auto_close_and_returns_summary")

    summary = agent._auto_close_ephemeral_tasks(log)

    assert summary is not None
    assert "1 ephemeral task(s) auto-closed" in summary
    assert "1 persistent task(s) still open" in summary
    saved_by_id = {t["id"]: t for t in _read()}
    assert saved_by_id[1]["status"] == "auto_closed"
    assert saved_by_id[2]["status"] == "in_progress"  # persistent untouched


def test_wire_in_returns_none_when_nothing_to_report(tasks_cwd):
    """No open tasks at startup ⇒ no summary (no preamble to inject)."""
    import agent
    _seed([
        {"id": 1, "description": "old", "status": "done"},
    ])
    log = logging.getLogger("test_wire_in_returns_none_when_nothing_to_report")

    summary = agent._auto_close_ephemeral_tasks(log)

    assert summary is None


def test_wire_in_swallows_unexpected_exceptions(monkeypatch, tasks_cwd):
    """A raising helper must not break startup — returns None, logs WARNING."""
    import agent
    import tools.task_tracker as _tt
    monkeypatch.setattr(_tt, "auto_close_ephemeral",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    log = logging.getLogger("test_wire_in_swallows_unexpected_exceptions")

    summary = agent._auto_close_ephemeral_tasks(log)

    assert summary is None
