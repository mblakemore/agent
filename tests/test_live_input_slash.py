"""A slash command typed during a tool burst is dispatched, not sent to the model.

The live-input thread puts every typed line on the bus as ("user", text). At an idle
boundary the consumer dispatches slash lines; mid-burst, `_drain_monitor_injections`
used to flatten every item to text and hand it to the model, so `/model NAME` typed
while tools ran became conversation text. Now, while the live-input consumer runs
(`_SLASH_HOLD["active"]`), such a line is held on the bus, in order, for the boundary.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import monitor_bus


def setup_function(_f):
    monitor_bus.drain()
    agent._SLASH_HOLD["active"] = True


def teardown_function(_f):
    monitor_bus.drain()
    agent._SLASH_HOLD["active"] = False


def test_slash_line_is_held_for_the_boundary_and_the_ping_is_injected():
    monitor_bus.put_user("/model main some-model")
    monitor_bus.inject("ping from a monitor")
    hist = []
    assert agent._drain_monitor_injections(hist, framing=agent._BTW_FRAMING) is True
    assert len(hist) == 1 and "ping from a monitor" in hist[0]["content"]
    assert "/model" not in hist[0]["content"]
    assert monitor_bus.get_blocking(timeout=1) == ("user", "/model main some-model")
    assert not monitor_bus.has_pending()


def test_slash_line_alone_injects_nothing():
    monitor_bus.put_user("  /verbose")
    hist = []
    assert agent._drain_monitor_injections(hist) is False
    assert hist == []
    assert monitor_bus.get_blocking(timeout=1) == ("user", "  /verbose")


def test_held_lines_keep_their_order_with_the_exit_mark():
    monitor_bus.put_user("/model main x")
    monitor_bus.put_user("a real message")
    monitor_bus.put_user(agent._LIVE_EXIT_MARK)
    hist = []
    assert agent._drain_monitor_injections(hist) is True
    assert hist[0]["content"] == "a real message"
    assert monitor_bus.get_blocking(timeout=1) == ("user", "/model main x")
    assert monitor_bus.get_blocking(timeout=1) == ("user", agent._LIVE_EXIT_MARK)


def test_a_monitor_ping_starting_with_a_slash_is_still_injected():
    monitor_bus.inject("/tmp/build.log changed")
    hist = []
    assert agent._drain_monitor_injections(hist) is True
    assert hist[0]["content"] == "/tmp/build.log changed"


def test_without_the_live_consumer_a_slash_line_is_text_as_before():
    agent._SLASH_HOLD["active"] = False
    monitor_bus.put_user("/model main x")
    hist = []
    assert agent._drain_monitor_injections(hist) is True
    assert hist[0]["content"] == "/model main x"
    assert not monitor_bus.has_pending()


def test_drain_items_keeps_kinds_and_drain_flattens():
    monitor_bus.put_user("typed")
    monitor_bus.inject("pinged")
    assert monitor_bus.drain_items() == [("user", "typed"), ("monitor", "pinged")]
    monitor_bus.put_user("typed")
    assert monitor_bus.drain() == ["typed"]
