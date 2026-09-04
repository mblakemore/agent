"""F9 — capability-refusal stop.

A tool that says the run was never GRANTED something is a fact about the run, not an error to
fix. The measured failure: refusal at turn 2, two hand-written fetchers, 43 more turns, no
result. These tests pin the three parts: the classifier, the bounded stop, and the synthesized
`blocked` record.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402
from agent import _classify_tool_refusal, _synthesize_result, run_agent_single  # noqa: E402

_REFUSAL = ("[session: s1] exit=2\n"
            "web-scrape: REFUSED — SOME_API_KEY not in environment (the job must grant it)")


class TestClassifier:
    def test_belt_style_refusal_on_nonzero_exit(self):
        code, head = _classify_tool_refusal("exec_command", _REFUSAL)
        assert code == 2 and "REFUSED" in head and "SOME_API_KEY" in head

    @pytest.mark.parametrize("text", [
        "[session: s1] exit=1\nusage: tool --url URL",                # usage, not capability
        "[session: s1] exit=0\npermission denied appears in this doc",  # exit 0 is never a refusal
        "[session: s1] exit=1\nTraceback (most recent call last): KeyError: 'foo'",
    ])
    def test_non_refusals(self, text):
        assert _classify_tool_refusal("exec_command", text) is None

    def test_a_document_the_run_read_is_not_a_refusal(self):
        """The words are the same; the source is not. Only a tool's OWN failure counts."""
        assert _classify_tool_refusal("read_file", "permission denied\nEACCES\nnot granted") is None
        assert _classify_tool_refusal("search_files", "a.py:3: raise PermissionError('access denied')") is None

    @pytest.mark.parametrize("text", [
        "[session: s1] exit=1\ncurl: (7) Failed to connect to host port 443: Connection refused",
        "[session: s1] exit=1\nError: 403 Forbidden",
        "[session: s1] exit=1\nrate limited: too many requests, retry later",
        "[session: s1] exit=3\nenvironment variable API_TOKEN is not set",
    ])
    def test_other_capability_shapes(self, text):
        assert _classify_tool_refusal("exec_command", text) is not None

    def test_network_tool_is_classified_on_text(self):
        assert _classify_tool_refusal("web_fetch", "HTTP 403 Forbidden for https://x") is not None
        assert _classify_tool_refusal("web_fetch", "<html>ok</html>") is None


class TestSynthesizedRecord:
    def test_no_refusal_synthesizes_failed(self, monkeypatch):
        monkeypatch.setattr(_agent, "_LAST_REFUSAL", None)
        assert _synthesize_result("nothing")["status"] == "failed"

    def test_refusal_synthesizes_blocked_and_names_the_tool(self, monkeypatch):
        monkeypatch.setattr(_agent, "_LAST_REFUSAL",
                            {"tool": "exec_command", "cmd": "node scrape.js", "exit": 2,
                             "head": "scrape: REFUSED — KEY not in environment", "turn": 2})
        rec = _synthesize_result("no block")
        assert rec["status"] == "blocked" and rec["synthesized"] is True
        assert "scrape.js" in rec["summary"] and "exit 2" in rec["summary"] and "turn 2" in rec["summary"]


# ── loop level (same harness as the F4/F7 tests) ─────────────────────────────
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


def _exec(i, cmd):
    return {"index": 0, "id": f"e{i}", "function": {"name": "exec_command",
                                                     "arguments": json.dumps({"command": cmd})}}


@pytest.fixture
def cycle_cfg():
    snap = json.loads(json.dumps(_agent._config["cycle"]))
    yield _agent._config["cycle"]
    _agent._config["cycle"].clear()
    _agent._config["cycle"].update(snap)
    _agent._LAST_EXIT = None
    _agent._LAST_REFUSAL = None


def _tool_msgs(history):
    return [m for m in history if m.get("role") == "tool"]


class TestBoundedStop:
    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_refusal_notice_once_then_tool_calls_refused(self, mock_llm, _emit, cycle_cfg):
        """Refused at turn 1 with a 3-turn window: the notice rides the refusing result exactly
        once; by turn 4 tool calls are refused; a run that never answers is stopped."""
        cycle_cfg["refusal_max_turns"] = 3
        mock_llm.side_effect = [_resp(tool_calls=[_exec(i, "node scrape.js --url u")]) for i in range(1, 8)] \
            + [_resp(content="final")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._AUTO_MODE", True), patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: _REFUSAL}):
            rc = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        tools = _tool_msgs(history)
        notices = [m for m in tools if "[CAPABILITY REFUSAL]" in m["content"]]
        refused = [m for m in tools if m["content"].startswith("REFUSED:")]
        assert len(notices) == 1, "one notice, in the refusing result, not a nag"
        assert "scrape.js" in notices[0]["content"] and "exited 2" in notices[0]["content"]
        assert refused, "past the window, tool calls are refused"
        assert all("blocked" in m["content"] for m in refused)
        assert _agent._LAST_REFUSAL and _agent._LAST_REFUSAL["turn"] == 1
        assert rc == "done"
        # the window is bounded: the run did not consume every scripted turn
        assert mock_llm.call_count < 8

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_blocked_result_inside_the_window_is_accepted(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["refusal_max_turns"] = 3
        cycle_cfg["result_contract"] = True
        block = json.dumps({"contract": 1, "status": "blocked",
                            "summary": "scrape.js exited 2: SOME_API_KEY not in environment"})
        mock_llm.side_effect = [_resp(tool_calls=[_exec(1, "node scrape.js --url u")]),
                                _resp(content=f"Cannot proceed.\n```json\n{block}\n```")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._AUTO_MODE", True), patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: _REFUSAL}):
            rc = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert rc == "done"
        assert not [m for m in _tool_msgs(history) if m["content"].startswith("REFUSED:")]
        assert mock_llm.call_count == 2

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_zero_disables_the_stop(self, mock_llm, _emit, cycle_cfg):
        """Control: with the window off, a refusal is just a tool result."""
        cycle_cfg["refusal_max_turns"] = 0
        mock_llm.side_effect = [_resp(tool_calls=[_exec(i, "node scrape.js")]) for i in range(1, 6)] \
            + [_resp(content="final")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._AUTO_MODE", True), patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: _REFUSAL}):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        tools = _tool_msgs(history)
        assert not [m for m in tools if "[CAPABILITY REFUSAL]" in m["content"]]
        assert not [m for m in tools if m["content"].startswith("REFUSED:")]
        assert mock_llm.call_count == 6

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_interactive_mode_is_untouched(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["refusal_max_turns"] = 3
        mock_llm.side_effect = [_resp(tool_calls=[_exec(i, "node scrape.js")]) for i in range(1, 6)] \
            + [_resp(content="final")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._AUTO_MODE", False), patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: _REFUSAL}):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert not [m for m in _tool_msgs(history) if "[CAPABILITY REFUSAL]" in m["content"]]


class TestSuccessGateHonoursBlocked:
    """The success-check gate polices 'done while RED'. A run that reports blocked is not
    claiming done; turning it back asks it to manufacture a pass. Measured on a real run:
    blocked at turn 5, turned back, five more turns writing placeholder files."""
    _BLOCK = json.dumps({"contract": 1, "status": "blocked",
                         "summary": "scrape.js exited 2: SOME_API_KEY not in environment"})

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_blocked_claim_passes_a_failing_success_check(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["success_check"] = "false"          # a check that always fails
        cycle_cfg["refusal_max_turns"] = 5
        mock_llm.side_effect = [_resp(tool_calls=[_exec(1, "node scrape.js --url u")]),
                                _resp(content=f"Cannot proceed.\n```json\n{self._BLOCK}\n```"),
                                _resp(content="should not be asked")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._AUTO_MODE", True), patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: _REFUSAL}):
            rc = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert rc == "done"
        assert not [m for m in history if m.get("role") == "user" and "still FAILS" in str(m.get("content"))]
        assert mock_llm.call_count == 2

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_done_claim_is_still_turned_back(self, mock_llm, _emit, cycle_cfg):
        """Control: the gate still does its job on a run that claims done while the check fails."""
        cycle_cfg["success_check"] = "false"
        done = json.dumps({"contract": 1, "status": "done", "summary": "all good"})
        # The gate turns a done-while-RED claim back up to three times before letting it go.
        mock_llm.side_effect = [_resp(content=f"Finished.\n```json\n{done}\n```") for _ in range(6)]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._AUTO_MODE", True), patch("agent._NUDGE_ENABLED", False):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        turned_back = [m for m in history if m.get("role") == "user" and "still FAILS" in str(m.get("content"))]
        assert turned_back, "a done claim against a failing check is still turned back"
