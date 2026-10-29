import json

import monitor_bus
import schedule_bus
from tools import schedule as schedule_tool


def test_schedule_fires_persists_and_removes(tmp_path, monkeypatch):
    f = tmp_path / "schedules.json"
    monkeypatch.setenv("AGENT_SCHEDULES_FILE", str(f))
    monitor_bus.drain()
    out = schedule_tool.fn(action="add", label="cycle", every_minutes=30, prompt="Run a light cycle.",
                           first_in_minutes=10)
    assert "added" in out and "not as a message from a person" in out
    assert "cycle" in json.loads(f.read_text())
    assert schedule_bus.fire_due() == []                      # not due yet
    import time
    assert schedule_bus.fire_due(now=time.time() + 11 * 60) == ["cycle"]
    assert monitor_bus.drain() == ["[scheduled:cycle] Run a light cycle."]
    assert schedule_bus.fire_due(now=time.time() + 12 * 60) == []   # next one is 30 min out
    assert schedule_bus.restore() == ["cycle"]                # survives a restart
    assert "Error" in schedule_tool.fn(action="add", label="x", every_minutes=0.1, prompt="p")
    assert "removed" in schedule_tool.fn(action="remove", label="cycle")
    assert schedule_tool.fn(action="list") == "No schedules."


def test_overdue_after_downtime_fires_once(tmp_path, monkeypatch):
    f = tmp_path / "schedules.json"
    monkeypatch.setenv("AGENT_SCHEDULES_FILE", str(f))
    f.write_text(json.dumps({"c": {"every_minutes": 1, "prompt": "p", "next_due": 1}}))
    monitor_bus.drain()
    schedule_bus.fire_due()
    schedule_bus.fire_due()
    assert monitor_bus.drain() == ["[scheduled:c] p"]
