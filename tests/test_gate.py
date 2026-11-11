"""The gate: a brief, a judge on the summary slot, and held digests that are never dropped.

A fake judge stands in for the summary model. The guarantees: regex bypass and quiet
never call the judge; a wake carries the digest of what was held in front of it; any
judge failure wakes; held items flush on the timer, on the size cap, on a flush, and
ride along on a wake that is already happening.
"""

import time

import pytest

import gate
import monitor_bus
from tools.gate import fn as gate_fn
from tools.monitor import fn as monitor_fn


class FakeJudge:
    """Scripted verdicts; records every prompt it saw."""

    def __init__(self, *verdicts):
        self.script = list(verdicts)
        self.prompts = []

    def __call__(self, prompt, timeout):
        self.prompts.append(prompt)
        item = self.script.pop(0) if self.script else "hold"
        if isinstance(item, Exception):
            raise item
        if item in ("wake", "hold"):
            return f'{{"verdict": "{item}", "why": "scripted", "line": "summary of it"}}'
        return item  # raw text, for the unparseable case


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    def _reset():
        monitor_bus.stop_all()
        for _ in range(6):
            time.sleep(0.03)
            monitor_bus.drain()
    _reset()
    monitor_bus.configure(session_events=True,
                          gate={"enabled": True, "log_dir": str(tmp_path / "gate")}, judge=None)
    yield
    _reset()
    monitor_bus.configure(gate={})


def _src(label="s", brief="wake me for errors", bypass="", quiet=""):
    monitor_bus.arm(label, "true", interval=3600, brief=brief, bypass=bypass, quiet=quiet)
    return monitor_bus.source(label)


# ── judge-free paths ──────────────────────────────────────────────────────
def test_bypass_wakes_without_a_judge_call():
    j = FakeJudge()
    monitor_bus.configure(judge=j)
    _src(bypass=r"\bboss\b")
    v = monitor_bus.emit("s", "boss: are you there?")
    assert v.verdict == "wake" and v.how == "bypass"
    assert monitor_bus.drain() == ["boss: are you there?"] and j.prompts == []


def test_quiet_holds_without_a_judge_call():
    j = FakeJudge()
    monitor_bus.configure(judge=j)
    _src(quiet=r'"type": "presence"')
    v = monitor_bus.emit("s", '{"type": "presence", "who": "x"}')
    assert v.verdict == "hold" and v.how == "quiet"
    assert monitor_bus.drain() == [] and j.prompts == []
    assert len(monitor_bus.source("s").held) == 1


def test_an_ungated_source_never_touches_the_judge():
    j = FakeJudge()
    monitor_bus.configure(judge=j)
    monitor_bus.arm("plain", "true", interval=3600)
    monitor_bus.emit("plain", "hello")
    assert monitor_bus.drain() == ["hello"] and j.prompts == []


# ── the judge ─────────────────────────────────────────────────────────────
def test_three_holds_then_a_wake_arrive_as_one_digest_plus_the_item():
    j = FakeJudge("hold", "hold", "hold", "wake")
    monitor_bus.configure(judge=j)
    _src()
    for t in ("progress 1", "progress 2", "progress 3"):
        monitor_bus.emit("s", t)
    assert monitor_bus.drain() == []
    monitor_bus.emit("s", "ERROR: boom")
    items = monitor_bus.drain()
    assert len(items) == 2
    assert items[0].startswith("[digest:s] 3 held items") and "summary of it" in items[0]
    assert "raw text:" in items[0]
    assert items[1] == "ERROR: boom"
    assert len(j.prompts) == 4 and "wake me for errors" in j.prompts[0]
    # the judge saw what was already held
    assert "summary of it" in j.prompts[3]


def test_the_raw_held_text_is_kept_on_disk(tmp_path):
    monitor_bus.configure(judge=FakeJudge("hold"))
    _src()
    monitor_bus.emit("s", "a long progress line")
    path = monitor_bus.source("s").raw_path
    assert path and open(path).read().strip().endswith("a long progress line")


def test_a_judge_that_raises_wakes():
    monitor_bus.configure(judge=FakeJudge(TimeoutError("slow")))
    _src()
    v = monitor_bus.emit("s", "something")
    assert v.verdict == "wake" and v.how == "failopen"
    assert monitor_bus.drain() == ["something"]


def test_an_unreadable_answer_wakes():
    monitor_bus.configure(judge=FakeJudge("I think you should hold this one"))
    _src()
    assert monitor_bus.emit("s", "x").how == "failopen"
    assert monitor_bus.drain() == ["x"]


def test_no_judge_configured_wakes():
    monitor_bus.configure(judge=None)
    _src()
    assert monitor_bus.emit("s", "x").how == "failopen"
    assert monitor_bus.drain() == ["x"]


# ── flushing ──────────────────────────────────────────────────────────────
def test_held_items_ride_along_on_any_other_wake():
    monitor_bus.configure(judge=FakeJudge("hold", "hold"))
    _src()
    monitor_bus.emit("s", "p1")
    monitor_bus.emit("s", "p2")
    monitor_bus.inject("[other] something else happened")
    items = monitor_bus.drain()
    assert items[0] == "[other] something else happened"
    assert items[1].startswith("[digest:s] 2 held items") and "rode along" in items[1]
    assert monitor_bus.source("s").held == []


def test_an_empty_queue_does_not_wake_for_digests_alone():
    monitor_bus.configure(judge=FakeJudge("hold"))
    _src()
    monitor_bus.emit("s", "p1")
    assert monitor_bus.drain() == []
    assert len(monitor_bus.source("s").held) == 1


def test_the_size_cap_flushes_a_digest():
    monitor_bus.configure(gate={"enabled": True, "max_held_chars": 20}, judge=FakeJudge())
    _src()
    monitor_bus.emit("s", "x" * 15)
    assert monitor_bus.drain() == []
    monitor_bus.emit("s", "y" * 15)
    items = monitor_bus.drain()
    assert len(items) == 1 and items[0].startswith("[digest:s] 2 held items") and "full" in items[0]


def test_the_timer_flushes_a_digest(monkeypatch):
    monkeypatch.setattr(monitor_bus, "_SWEEP_S", 0.1)
    monitor_bus.configure(gate={"enabled": True, "digest_after_s": 0.3}, judge=FakeJudge())
    _src()
    monitor_bus.emit("s", "p1")
    deadline = time.time() + 5
    items = []
    while time.time() < deadline and not items:
        items = monitor_bus.drain()
        time.sleep(0.05)
    assert items and items[0].startswith("[digest:s] 1 held item") and "held over" in items[0]


def test_digest_raw_chars_includes_the_raw_text():
    monitor_bus.configure(gate={"enabled": True, "digest_raw_chars": 500}, judge=FakeJudge("hold", "wake"))
    _src()
    monitor_bus.emit("s", "the raw progress line")
    monitor_bus.emit("s", "go")
    items = monitor_bus.drain()
    assert "raw:\nthe raw progress line" in items[0]


# ── the tools ─────────────────────────────────────────────────────────────
def test_monitor_arm_with_a_brief_and_the_gate_tool_status_and_flush():
    monitor_bus.configure(judge=FakeJudge("hold"))
    out = monitor_fn("arm", label="ev", command="true", interval_seconds=3600,
                     brief="wake me when someone says my name", quiet="presence")
    assert "gated" in out
    monitor_bus.emit("ev", "presence changed")
    status = gate_fn("status")
    assert "ev (monitor" in status and "1 held" in status and "my name" in status
    assert "Flushed held items of 1 source" in gate_fn("flush", source="ev")
    assert monitor_bus.drain()[0].startswith("[digest:ev]")


def test_gate_set_changes_the_brief_and_off_removes_it():
    monitor_bus.configure(judge=FakeJudge("hold"))
    monitor_bus.arm("ev", "true", interval=3600)
    assert "gated" in gate_fn("set", source="ev", brief="only errors", bypass="boss")
    assert monitor_bus.source("ev").brief == "only errors"
    monitor_bus.emit("ev", "noise")
    assert "no longer gated" in gate_fn("off", source="ev")
    assert monitor_bus.drain()[0].startswith("[digest:ev]")
    monitor_bus.emit("ev", "more")
    assert monitor_bus.drain() == ["more"]


def test_gate_tool_refuses_when_disabled():
    monitor_bus.configure(gate={"enabled": False})
    assert gate_fn("status").startswith("Error: the gate is off")
    assert monitor_fn("arm", label="x", command="true", brief="b").startswith("Error")


def test_a_persisted_monitor_keeps_its_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MONITORS_FILE", str(tmp_path / "m.json"))
    monitor_bus.arm("ev", "true", interval=3600, persist=True, brief="errors only", quiet="tick")
    monitor_bus.set_gate("ev", bypass="urgent")
    monitor_bus.stop_all()
    assert monitor_bus.restore_persisted() == ["ev"]
    src = monitor_bus.source("ev")
    assert src.brief == "errors only" and src.quiet_src == "tick" and src.bypass_src == "urgent"


# ── gate.py on its own ────────────────────────────────────────────────────
def test_config_from_tolerates_junk():
    cfg = gate.config_from({"enabled": "yes", "timeout_s": "x", "max_held_chars": -5})
    assert cfg["enabled"] is True and cfg["timeout_s"] == 20.0 and cfg["max_held_chars"] == 0


def test_parse_answer_finds_the_object_inside_chatter():
    assert gate.parse_answer('Sure: {"verdict": "WAKE", "why": "err", "line": "l"} ok')[0] == "wake"
    assert gate.parse_answer('{"verdict": "drop"}') is None
    assert gate.parse_answer("") is None
