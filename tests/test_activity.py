"""The loop's activity record: what the F2 key prints, from the hooks the turn loop calls."""
import activity


def setup_function(_):
    activity.reset()


def test_idle_then_turn_request_tool():
    assert activity.describe(now=0.0).startswith("turn 0 · idle")
    activity.turn_started(3)
    activity.request_started("m-1")
    text = activity.describe()
    assert text.startswith("turn 3 · waiting on m-1 for")
    activity.response_text("I will read the file now.")
    activity.tool_started("exec_command", {"command": "cat pytest.ini; ls tests | wc -l", "background": True})
    text = activity.describe()
    assert "exec_command running for" in text and "(call 1 this session)" in text
    assert '"command": "cat pytest.ini; ls tests | wc -l"' in text        # the full arguments
    assert "last said" in text and "I will read the file now." in text
    activity.tool_finished("exec_command", 2046)
    text = activity.describe()
    assert "between steps" in text and "last tool result: exec_command, 2,046 chars" in text
    assert "arguments:" not in text
    activity.idle()
    assert "idle, waiting for input" in activity.describe()


def test_long_arguments_are_capped_not_dropped(monkeypatch):
    monkeypatch.setattr(activity, "ARGS_MAX_CHARS", 50)
    activity.tool_started("write_file", {"content": "x" * 500})
    text = activity.describe()
    assert "more chars" in text and "x" * 20 in text


def test_describe_never_raises_on_odd_args():
    activity.tool_started("t", {"f": object()})
    assert "running for" in activity.describe()
