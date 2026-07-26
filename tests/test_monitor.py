"""Background monitor bus + `monitor` tool + loop drain helper.

The load-bearing guarantee: with no monitor armed, the loop drain is a no-op
and the conversation is untouched — so the agentic loop is unchanged unless a
monitor is explicitly armed. That is tested first.
"""

import time

import pytest

import agent as _agent
import monitor_bus
from tools.monitor import fn as monitor_fn


@pytest.fixture(autouse=True)
def _clean_bus():
    """Every test starts and ends with no monitors / empty queue.

    Drains repeatedly over a short window so an in-flight immediate poll that
    lands just after stop_all() can't leak into the next test.
    """
    def _reset():
        monitor_bus.stop_all()
        for _ in range(12):
            time.sleep(0.05)
            monitor_bus.drain()
    _reset()
    yield
    _reset()


def _wait_drain(timeout=4.0):
    end = time.time() + timeout
    got = []
    while time.time() < end:
        got += monitor_bus.drain()
        if got:
            return got
        time.sleep(0.05)
    return got


# ── blast-radius: drain is a no-op when nothing is armed ──────────────────
def test_drain_helper_noop_when_empty():
    history = [{"role": "user", "content": "hi"}]
    before = list(history)
    assert _agent._drain_monitor_injections(history) is False
    assert history == before  # byte-for-byte untouched


def test_drain_helper_injects_and_reports_true():
    monitor_bus._BUS._put("[monitor:x] hello")
    history = []
    assert _agent._drain_monitor_injections(history) is True
    assert history == [{"role": "user", "content": "[monitor:x] hello"}]
    # queue now empty -> next drain is a no-op
    assert _agent._drain_monitor_injections(history) is False


# ── bus: arm / inject / dedup / disarm ────────────────────────────────────
def test_arm_injects_command_output():
    r = monitor_bus.arm("t", "printf 'a mention'", interval=2, prefix="[m] ")
    assert r["ok"] and r["replaced"] is False
    got = _wait_drain()
    assert got and got[0] == "[m] a mention"


def test_empty_output_is_not_injected():
    monitor_bus.arm("t", "true", interval=2)  # exits 0, no stdout
    assert _wait_drain(timeout=1.5) == []


def test_nonzero_exit_is_not_injected():
    monitor_bus.arm("t", "echo boom >&2; exit 1", interval=2)
    assert _wait_drain(timeout=1.5) == []


def test_dedup_suppresses_identical_consecutive():
    # interval 2s: immediate poll + one more within the window -> still 1 item.
    monitor_bus.arm("t", "echo same", interval=2, prefix="", dedup=True)
    time.sleep(2.6)
    got = monitor_bus.drain()
    assert got == ["same"]  # second identical poll deduped


def test_disarm_stops_injection():
    monitor_bus.arm("t", "printf x", interval=2, prefix="")
    assert _wait_drain()  # got the first
    assert monitor_bus.disarm("t")["ok"] is True
    monitor_bus.drain()
    # nothing new after disarm
    assert _wait_drain(timeout=2.5) == []


def test_replace_same_label():
    monitor_bus.arm("t", "printf a", interval=2)
    r = monitor_bus.arm("t", "printf b", interval=2)
    assert r["replaced"] is True
    assert len(monitor_bus.list_active()) == 1  # not two


def test_cmd_timeout_clamped_to_interval():
    # A poll that hangs longer than the interval must not wedge: cmd_timeout is
    # clamped to <= interval, so the hung command is killed and nothing queued.
    monitor_bus.arm("t", "sleep 5", interval=2, cmd_timeout=999)
    assert _wait_drain(timeout=3.0) == []


def test_max_monitors_ceiling():
    for i in range(monitor_bus._MAX_MONITORS):
        assert monitor_bus.arm(f"m{i}", "true", interval=5)["ok"]
    over = monitor_bus.arm("one-too-many", "true", interval=5)
    assert over["ok"] is False and "too many" in over["error"]


def test_stop_all_does_not_block():
    monitor_bus.arm("t", "sleep 5", interval=2, cmd_timeout=2)
    t0 = time.time()
    monitor_bus.stop_all()
    # must return immediately (no thread.join on a mid-subprocess monitor)
    assert time.time() - t0 < 0.5
    assert monitor_bus.list_active() == []


# ── the `monitor` tool surface ────────────────────────────────────────────
def test_tool_list_empty():
    assert monitor_fn(action="list") == "No active monitors."


def test_tool_arm_requires_label_and_command():
    assert "label" in monitor_fn(action="arm", command="echo x").lower()
    assert "command" in monitor_fn(action="arm", label="x").lower()


def test_tool_arm_then_list_then_stop():
    out = monitor_fn(action="arm", label="mentions",
                     command="printf hi", interval_seconds=3)
    assert "armed" in out.lower()
    listing = monitor_fn(action="list")
    assert "mentions" in listing
    stopped = monitor_fn(action="stop", label="mentions")
    assert "stopped" in stopped.lower()


def test_tool_unknown_action():
    assert "unknown action" in monitor_fn(action="frobnicate").lower()


# ── idle-wake plumbing (notifier + has_pending + safe wake) ───────────────
def test_notifier_fires_on_put_and_has_pending():
    hits = []
    monitor_bus.set_notifier(lambda: hits.append(1))
    try:
        assert monitor_bus.has_pending() is False
        monitor_bus._BUS._put("x")
        assert monitor_bus.has_pending() is True
        assert hits == [1]
    finally:
        monitor_bus.clear_notifier()
        monitor_bus.drain()


def test_cleared_notifier_does_not_fire():
    hits = []
    monitor_bus.set_notifier(lambda: hits.append(1))
    monitor_bus.clear_notifier()
    monitor_bus._BUS._put("x")
    monitor_bus.drain()
    assert hits == []


def test_notifier_exception_is_swallowed():
    def boom():
        raise RuntimeError("notifier blew up")
    monitor_bus.set_notifier(boom)
    try:
        monitor_bus._BUS._put("x")  # must not propagate
        assert monitor_bus.has_pending()
    finally:
        monitor_bus.clear_notifier()
        monitor_bus.drain()


def test_tui_wake_safe_noop_when_not_prompting():
    """wake() from a monitor thread must be a safe no-op when no prompt is
    active (the full idle-wake path is validated live in a real TTY)."""
    import tui
    if not getattr(tui, "_AVAILABLE", False):
        pytest.skip("prompt_toolkit not available")
    from unittest.mock import MagicMock
    sess = tui.TuiSession(history=[], summary_state={}, config={}, ctx_size=8000,
                          cb=MagicMock(), estimate_tokens=lambda m: 0)
    assert sess.WAKE is tui.MONITOR_WAKE
    sess.wake()  # not prompting -> no exception, no effect
