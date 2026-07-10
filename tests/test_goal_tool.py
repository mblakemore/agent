"""WS2 wave-2 tests: goal stack tool + preamble hydration + goal_id linkage."""

import json
import os

import pytest

from tools import goal as goal_tool


@pytest.fixture
def goals_cwd(tmp_path, monkeypatch):
    """Run each test in an isolated cwd so state/goals.json is fresh."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestGoalCrud:
    def test_add_and_list(self, goals_cwd):
        out = goal_tool.fn(action="add", title="Ship replay suite v0")
        assert "g001 created" in out
        out = goal_tool.fn(action="list")
        assert "Ship replay suite v0" in out and "active" in out

    def test_steps_flow_and_autocomplete(self, goals_cwd):
        goal_tool.fn(action="add", title="G")
        goal_tool.fn(action="step-add", goal_id="g001", description="step one")
        goal_tool.fn(action="step-add", goal_id="g001", description="step two")
        out = goal_tool.fn(action="step-done", goal_id="g001", step_id="s01",
                           outcome="tests green")
        assert "Next: (s02)" in out
        out = goal_tool.fn(action="step-done", goal_id="g001", step_id="s02")
        assert "goal g001 marked done" in out
        data = json.loads(open("state/goals.json").read())
        assert data["goals"][0]["status"] == "done"
        assert data["goals"][0]["steps"][0]["outcome"] == "tests green"

    def test_unknown_goal_and_action_errors(self, goals_cwd):
        assert "not found" in goal_tool.fn(action="status", goal_id="g999")
        goal_tool.fn(action="add", title="G")
        assert "unknown action" in goal_tool.fn(action="zap", goal_id="g001")

    def test_plan_attach_missing_file_rejected(self, goals_cwd):
        goal_tool.fn(action="add", title="G")
        out = goal_tool.fn(action="plan", goal_id="g001",
                           plan_path="PLAN-missing.md")
        assert "Error" in out and "not found" in out

    def test_complete_abandoned(self, goals_cwd):
        goal_tool.fn(action="add", title="G")
        out = goal_tool.fn(action="complete", goal_id="g001",
                           status="abandoned")
        assert "abandoned" in out


class TestPreambleSummary:
    def test_empty_without_file(self, goals_cwd):
        assert goal_tool.preamble_summary() == ""

    def test_active_goal_shows_next_step(self, goals_cwd):
        goal_tool.fn(action="add", title="Long arc")
        goal_tool.fn(action="step-add", goal_id="g001", description="do X")
        txt = goal_tool.preamble_summary()
        assert "GOAL STACK" in txt
        assert "Long arc" in txt
        assert "next: (s01) do X" in txt

    def test_done_goals_excluded(self, goals_cwd):
        goal_tool.fn(action="add", title="Done arc")
        goal_tool.fn(action="complete", goal_id="g001")
        assert goal_tool.preamble_summary() == ""

    def test_char_cap(self, goals_cwd):
        for i in range(60):
            goal_tool.fn(action="add", title="goal " + "x" * 80)
        txt = goal_tool.preamble_summary()
        assert len(txt) <= goal_tool._PREAMBLE_CHAR_CAP + 100
        assert "truncated" in txt

    def test_corrupt_file_safe(self, goals_cwd):
        os.makedirs("state", exist_ok=True)
        with open("state/goals.json", "w") as f:
            f.write("{not json")
        assert goal_tool.preamble_summary() == ""


class TestTaskTrackerGoalLink:
    def test_goal_id_stored_on_add(self, goals_cwd):
        from tools import task_tracker
        out = task_tracker.fn(action="add", description="linked task",
                              goal_id="g001")
        assert "Added task" in out
        tasks = json.loads(open(".agent/state/tasks.json").read())
        linked = [t for t in tasks if t.get("description") == "linked task"]
        assert linked and linked[0]["goal_id"] == "g001"

    def test_shape_unchanged_without_goal_id(self, goals_cwd):
        from tools import task_tracker
        task_tracker.fn(action="add", description="plain task")
        tasks = json.loads(open(".agent/state/tasks.json").read())
        plain = [t for t in tasks if t.get("description") == "plain task"]
        assert plain and "goal_id" not in plain[0]
