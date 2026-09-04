"""Headless hardening F4 (goal anchoring + deliverable guard) and F7 (repeat-read stall)."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402
from agent import (_derive_goal_and_deliverables, _goal_anchor_message, _missing_deliverables,  # noqa: E402
                   _repeat_read_check, run_agent_single)


# ── F7 pure ───────────────────────────────────────────────────────────────────
class TestRepeatReadCheck:
    def test_three_identical_reads_fire_once(self):
        seen, latched = {}, set()
        k = ("read_file", '{"path": "a.py"}')
        fired = [_repeat_read_check(seen, k, t, 3, 6, latched) for t in (1, 2, 3, 4, 5)]
        assert fired == [False, False, True, False, False]

    def test_three_different_reads_do_not_fire(self):
        seen, latched = {}, set()
        fired = [_repeat_read_check(seen, ("read_file", f'{{"path": "{p}"}}'), t, 3, 6, latched)
                 for t, p in enumerate(("a", "b", "c"), 1)]
        assert fired == [False, False, False]

    def test_window_expiry_resets_the_count(self):
        seen, latched = {}, set()
        k = ("read_file", "x")
        assert _repeat_read_check(seen, k, 1, 3, 6, latched) is False
        assert _repeat_read_check(seen, k, 2, 3, 6, latched) is False
        assert _repeat_read_check(seen, k, 20, 3, 6, latched) is False   # the first two aged out


# ── F4 pure ───────────────────────────────────────────────────────────────────
class TestGoalDerivation:
    def test_stanza_yields_goal_and_path_like_deliverables(self):
        g, d = _derive_goal_and_deliverables("WORK ITEM. GOAL: produce the report\nCONSTRAINTS: x\n"
                                             "DELIVERABLE: out/report.json, notes/*.md and a summary paragraph\n")
        assert g == "produce the report"
        assert d == ["out/report.json", "notes/*.md"]

    def test_no_stanza_yields_nothing(self):
        assert _derive_goal_and_deliverables("just do the thing") == ("", [])

    def test_missing_deliverables_is_mechanical(self, tmp_path):
        (tmp_path / "present.txt").write_text("x")
        missing = _missing_deliverables(["present.txt", "absent.txt", "sub/*.json"], cwd=str(tmp_path))
        assert missing == ["absent.txt", "sub/*.json"]

    def test_anchor_message_names_missing(self):
        m = _goal_anchor_message("ship it", ["a", "b"], ["b"], 0.5)
        assert "50%" in m and "ship it" in m and "do NOT exist yet: b" in m


# ── loop level ────────────────────────────────────────────────────────────────
def _resp(content=None, tool_calls=None):
    r = MagicMock()
    r.status_code = 200
    lines = []
    if tool_calls:
        for tc in tool_calls:
            lines.append(f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [tc]}}]})}".encode())
    elif content:
        lines.append(f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}".encode())
    lines.append(b"data: [DONE]")
    r.iter_lines.return_value = lines
    return r


def _read(i, path="a.py"):
    return {"index": 0, "id": f"r{i}", "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}


def _exec(i, cmd):
    return {"index": 0, "id": f"e{i}", "function": {"name": "exec_command", "arguments": json.dumps({"command": cmd})}}


@pytest.fixture
def cycle_cfg():
    snap = json.loads(json.dumps(_agent._config["cycle"]))
    yield _agent._config["cycle"]
    _agent._config["cycle"].clear()
    _agent._config["cycle"].update(snap)
    _agent._LAST_EXIT = None


def _texts(mock_llm, role):
    out = []
    for call in mock_llm.call_args_list:
        for m in call.kwargs["json"]["messages"]:
            if m.get("role") == role:
                out.append(str(m.get("content")))
    return out


def _system_texts(mock_llm):
    return _texts(mock_llm, "system")


def _notice_texts(mock_llm):
    """Mid-history notices (the F7 repeat-read nudge among them) are injected with the USER
    role, because strict chat templates reject a system message anywhere but first. The
    repeat-read tests filtered on role=system from the day they were written and so could
    never see the nudge: the "nudges once" assertion had never passed. Look where it lands."""
    return _texts(mock_llm, "user")


class TestRepeatReadLoop:
    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_same_read_three_times_nudges_once(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["repeat_read_nudge"] = {"n": 3, "window": 6}
        mock_llm.side_effect = [_resp(tool_calls=[_read(1)]), _resp(tool_calls=[_read(2)]),
                                _resp(tool_calls=[_read(3)]), _resp(tool_calls=[_read(4)]), _resp(content="final")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"read_file": lambda **kw: "contents"}):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        nudges = [s for s in _notice_texts(mock_llm) if "same read" in s]
        assert len(nudges) == 1, "one nudge, not a nag"

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_three_different_reads_do_not_nudge(self, mock_llm, _emit, cycle_cfg):
        mock_llm.side_effect = [_resp(tool_calls=[_read(1, "a")]), _resp(tool_calls=[_read(2, "b")]),
                                _resp(tool_calls=[_read(3, "c")]), _resp(content="final")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"read_file": lambda **kw: "contents"}):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert not [s for s in _notice_texts(mock_llm) if "same read" in s]
        assert not [s for s in _system_texts(mock_llm) if "same read" in s]


class TestDeliverableGuardLoop:
    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_done_with_missing_deliverable_is_corrected_then_accepted(self, mock_llm, _emit, cycle_cfg, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cycle_cfg["deliverables"] = ["out/result.json"]
        cycle_cfg["goal_anchor_fracs"] = []

        def make_it(**kw):
            os.makedirs(tmp_path / "out", exist_ok=True)
            (tmp_path / "out" / "result.json").write_text("{}")
            return "exit=0"
        mock_llm.side_effect = [_resp(content="Done — all good."),                       # premature done
                                _resp(tool_calls=[_exec(1, "make it")]),                # produces the file
                                _resp(content="Done, the deliverable exists now.")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": make_it}):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done" and mock_llm.call_count == 3
        corrections = [m for m in history if m.get("role") == "user" and "do not exist" in str(m.get("content"))]
        assert len(corrections) == 1 and "out/result.json" in corrections[0]["content"]

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_blocked_status_passes_untouched(self, mock_llm, _emit, cycle_cfg, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cycle_cfg["deliverables"] = ["never/made.txt"]
        cycle_cfg["goal_anchor_fracs"] = []
        blocked = ('```json\n{"contract": 1, "status": "blocked", "summary": "no access"}\n```')
        mock_llm.side_effect = [_resp(content="Cannot proceed.\n" + blocked)]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done" and mock_llm.call_count == 1

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_no_keys_means_zero_injections(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["goal"] = ""
        cycle_cfg["deliverables"] = []
        mock_llm.side_effect = [_resp(content="done")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert mock_llm.call_count == 1
        assert not [m for m in history if "GOAL ANCHOR" in str(m.get("content"))]

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_anchor_fires_at_turn_fraction_with_missing_list(self, mock_llm, _emit, cycle_cfg, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cycle_cfg["goal"] = "build the widget"
        cycle_cfg["deliverables"] = ["widget.py"]
        cycle_cfg["goal_anchor_fracs"] = [0.5]
        # no deadline → turn fraction of _MAX_TURNS; patch the turn budget small. The final
        # message reports BLOCKED (the deliverable is still absent, so a plain 'done' would be
        # correctly redirected by the guard — that case has its own test above).
        blocked = '```json\n{"contract": 1, "status": "blocked", "summary": "could not build it"}\n```'
        mock_llm.side_effect = [_resp(tool_calls=[_read(i, f"f{i}")]) for i in range(1, 4)] + [_resp(content=blocked)]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), patch("agent._MAX_TURNS", 4), \
             patch.dict("agent.MAP_FN", {"read_file": lambda **kw: "c"}):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        # USER role, not system: this message lands MID-HISTORY, and chat templates that
        # accept system only in the leading position reject a later one outright (Qwen3
        # returns HTTP 500, "System message must be at the beginning"). Asserting the role
        # here is not style — it is the thing that kept four headless runs alive.
        anchors = [m for m in history if m.get("role") == "user" and "GOAL ANCHOR" in str(m.get("content"))]
        # THE REAL INVARIANT, stronger than the role of the anchor itself: nothing may append
        # a system message after the conversation has begun. A future injection that reached
        # for "system" would pass the assertion above and still break every strict template.
        assert not [m for m in history[1:] if m.get("role") == "system"], \
            "no system message may appear mid-history — strict templates reject it"
        assert len(anchors) == 1 and "widget.py" in anchors[0]["content"] and "build the widget" in anchors[0]["content"]
