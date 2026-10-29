import json

import monitor_bus
from tools import monitor as monitor_tool


def test_persistent_monitor_survives_restart(tmp_path, monkeypatch):
    f = tmp_path / "monitors.json"
    monkeypatch.setenv("AGENT_MONITORS_FILE", str(f))
    monitor_bus.stop_all()
    out = monitor_tool.fn(action="arm", label="t1", command="echo hi", interval_seconds=60, persist=True)
    assert "re-armed when agent.py restarts" in out
    assert "t1" in json.loads(f.read_text())
    monitor_bus.stop_all()                       # simulated process exit: file stays
    assert monitor_bus.list_active() == []
    assert monitor_bus.restore_persisted() == ["t1"]
    assert [m["label"] for m in monitor_bus.list_active()] == ["t1"]
    monitor_tool.fn(action="stop", label="t1")   # stop un-persists
    assert json.loads(f.read_text()) == {}
    monitor_bus.stop_all()


def test_non_persistent_monitor_is_not_saved(tmp_path, monkeypatch):
    f = tmp_path / "monitors.json"
    monkeypatch.setenv("AGENT_MONITORS_FILE", str(f))
    monitor_tool.fn(action="arm", label="t2", command="echo hi", interval_seconds=60)
    assert not f.exists()
    monitor_bus.stop_all()
