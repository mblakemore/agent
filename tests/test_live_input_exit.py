"""Typed 'exit' during a streaming turn is CONTROL, not conversation.

Field report 2026-08-14: typing exit mid-stream killed the input box, and
the queued line was framed as a background note and delivered to the MODEL
by the mid-burst drain — while the interactive main loop (the only consumer
that knows what 'exit' means) never saw it and wedged on get_blocking().

The fix has two halves: the live-input thread enqueues _LIVE_EXIT_MARK
(never the raw line) and requests cancellation of the running turn; and
_drain_monitor_injections filters the mark out of any injection body,
re-enqueuing it so the interactive loop still breaks at the turn boundary.
These tests pin the drain-side contract.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import monitor_bus


def setup_function(_f):
    monitor_bus.drain()  # isolate: the bus is a module singleton


def teardown_function(_f):
    monitor_bus.drain()


def test_exit_mark_alone_injects_nothing_and_requeues():
    monitor_bus.put_user(agent._LIVE_EXIT_MARK)
    hist = []
    assert agent._drain_monitor_injections(hist) is False
    assert hist == []
    # Re-queued for the interactive main loop's turn-boundary consumer.
    assert monitor_bus.get_blocking(timeout=1) == ("user", agent._LIVE_EXIT_MARK)


def test_exit_mark_filtered_from_mixed_injection():
    monitor_bus.put_user("also check the tests")
    monitor_bus.put_user(agent._LIVE_EXIT_MARK)
    hist = []
    assert agent._drain_monitor_injections(hist, framing=agent._BTW_FRAMING) is True
    assert len(hist) == 1
    body = hist[0]["content"]
    assert agent._LIVE_EXIT_MARK not in body
    assert "also check the tests" in body
    assert monitor_bus.get_blocking(timeout=1) == ("user", agent._LIVE_EXIT_MARK)


def test_plain_items_unaffected():
    monitor_bus.put_user("hello")
    hist = []
    assert agent._drain_monitor_injections(hist) is True
    assert hist[0]["content"] == "hello"
    assert not monitor_bus.has_pending()


def test_mark_survives_repeated_boundaries():
    """run_agent_single hits the btw-drain at every turn boundary; the mark
    must survive N boundaries without being injected or lost, so the main
    loop still receives it when the (cancelled) turn finally returns."""
    monitor_bus.put_user(agent._LIVE_EXIT_MARK)
    for _ in range(3):
        hist = []
        assert agent._drain_monitor_injections(hist) is False
        assert hist == []
    assert monitor_bus.get_blocking(timeout=1) == ("user", agent._LIVE_EXIT_MARK)
