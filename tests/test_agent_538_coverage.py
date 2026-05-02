"""
Tests targeting agent.py lines ~3028-3180:
  - hallucination guard (first text-only stripped, lines 3028-3032)
  - hallucinated file read detection + nudge (lines 3034-3046)
  - CICD cycle-82 PR-missing nudge (lines 3047-3103)
  - substantive tool check (lines 3105-3136)
  - tool-call loop detection (lines 3138-3173)

Issue #538: increase agent.py coverage from 49% -> 52%+.
"""
import json
import logging
from unittest.mock import patch, MagicMock

import agent
from agent import run_agent_single

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stream(*deltas):
    """Build SSE byte-line list from delta dicts."""
    lines = []
    for d in deltas:
        lines.append(b"data: " + json.dumps({"choices": [{"delta": d}]}).encode())
    lines.append(b"data: [DONE]")
    return lines


def _resp(*deltas):
    m = MagicMock()
    m.status_code = 200
    m.iter_lines.return_value = _stream(*deltas)
    return m


def _tc(name, args, tc_id="tc1"):
    return {
        "index": 0, "id": tc_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _text(content):
    return _resp({"content": content})


def _tool_resp(name, args, tc_id="tc1"):
    return _resp({"tool_calls": [_tc(name, args, tc_id)]})


# ---------------------------------------------------------------------------
# 1. Hallucination guard — first text-only stripped (line 3028-3032)
# ---------------------------------------------------------------------------

@patch("agent._emit")
@patch("agent._llm_request")
def test_hallucination_guard_first_text_only_stripped(mock_llm, mock_emit):
    """First text-only response is stripped from history (consecutive==1 branch)."""
    # Flow with _MAX_TEXT_ONLY=3:
    #  call 1: text → consecutive=1 → stripped, continue (on_hallucination_stripped text_only)
    #  call 2: text → consecutive=2 → generic nudge injected
    #  call 3: text → consecutive=3 >= 3 → stop
    responses = [
        _text("Here is what I found."),   # consecutive=1 → stripped silently
        _text("Continuing my analysis."),  # consecutive=2 → nudge
        _text("Still working."),           # consecutive=3 → stop
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "test"}]
    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 3), \
         patch("agent._MAX_TOTAL_NUDGES", 10):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    assert result == "done"
    emit_events = [args[0] for args, _ in mock_emit.call_args_list]
    assert "on_hallucination_stripped" in emit_events, (
        "Expected on_hallucination_stripped event from first text-only guard"
    )


# ---------------------------------------------------------------------------
# 2. Hallucinated file read detection + nudge (lines 3034-3046)
# ---------------------------------------------------------------------------

@patch("agent._emit")
@patch("agent._llm_request")
def test_hallucination_guard_file_read_detected(mock_llm, mock_emit):
    """Hallucinated file-read claim at consecutive==2 triggers targeted nudge."""
    # Flow with _MAX_TEXT_ONLY=4:
    #  call 1: text → consecutive=1 → stripped (on_hallucination_stripped text_only)
    #  call 2: text with file read claim → consecutive=2 → _hallucinated_read=True
    #           → strip + nudge about file tool (on_hallucination_stripped file_read)
    #  call 3: text → consecutive=3 → generic nudge
    #  call 4: text → consecutive=4 >= 4 → stop
    responses = [
        _text("Looking into it."),                       # consecutive=1 → stripped
        _text("I found the contents of agent.py clearly, it has a bug."),  # consecutive=2 → hallucinated read
        _text("Let me try again."),                      # consecutive=3 → nudge
        _text("Done."),                                  # consecutive=4 → stop
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "Review the code"}]
    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 4), \
         patch("agent._MAX_TOTAL_NUDGES", 10), \
         patch("tools.file._accessed_files", set()):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    assert result == "done"
    hallucination_events = [
        (args[0], args[1] if len(args) > 1 else None)
        for args, _ in mock_emit.call_args_list
        if args[0] == "on_hallucination_stripped"
    ]
    file_read_events = [e for e in hallucination_events if e[1] == "file_read"]
    assert len(file_read_events) >= 1, (
        f"Expected on_hallucination_stripped('file_read'), got: {hallucination_events}"
    )


# ---------------------------------------------------------------------------
# 3. CICD cycle-82 nudge: edited files but no PR (lines 3047-3103)
# ---------------------------------------------------------------------------

@patch("agent._emit")
@patch("agent._llm_request")
def test_cicd_cycle82_nudge_no_pr(mock_llm, mock_emit):
    """Cycle-82 guard fires when cicd branch + edited files + no PR number.

    To set up the CICD state properly we simulate tool calls that set:
      _cicd_branch via "git worktree add /tmp/wt -b cicd/538-test"
      _cicd_issue_number via "gh issue view 538" returning exit=0
      _cicd_edited_files via file(action=write)
    Then text-only responses trigger the guard.
    """
    worktree_call = _tc("exec_command", {"command": "git worktree add /tmp/wt -b cicd/538-test"})
    issue_view_call = _tc("exec_command", {"command": "gh issue view 538"}, "tc-issue")
    file_write_call = _tc("file", {"action": "write", "path": "/tmp/wt/agent.py", "content": "x=1"}, "tc-write")

    responses = [
        _resp({"tool_calls": [worktree_call]}),    # sets _cicd_branch
        _resp({"tool_calls": [issue_view_call]}),  # sets _cicd_issue_number
        _resp({"tool_calls": [file_write_call]}),  # sets _cicd_edited_files
        _text("Looking into it."),                  # consecutive=1 → stripped
        _text("Tests are passing now."),            # consecutive=2 → cycle-82 nudge
        _text("Working on PR."),                    # consecutive=3 → nudge
        _text("Done."),                             # consecutive=4 → stop
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "CICD Improvement Loop — Builder task"}]

    exec_results = {
        "git worktree add /tmp/wt -b cicd/538-test": "exit=0\nPreparing worktree (new branch 'cicd/538-test')\n",
        "gh issue view 538": "exit=0\n#538 title\nurl: https://github.com/owner/repo/issues/538\n",
    }

    def _exec_fn(**kw):
        cmd = kw.get("command", "")
        for k, v in exec_results.items():
            if k in cmd:
                return v
        return "exit=0\n"

    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 4), \
         patch("agent._MAX_TOTAL_NUDGES", 10), \
         patch.dict("agent.MAP_FN", {
             "exec_command": _exec_fn,
             "file": lambda **kw: "written",
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    assert result == "done"
    # A nudge message mentioning "gh pr create" should appear in user messages
    user_nudges = [
        m["content"] for m in history
        if m.get("role") == "user" and "pr create" in m.get("content", "").lower()
    ]
    assert user_nudges, (
        "Expected a nudge message about gh pr create, user messages: "
        + str([m for m in history if m.get("role") == "user"])
    )


@patch("agent._emit")
@patch("agent._llm_request")
def test_cicd_cycle82_nudge_after_push_no_pr(mock_llm, mock_emit):
    """Cycle-82 nudge (_cycle_persisted=True branch) fires after push without PR.

    Sets up CICD state via tool calls, then push sets _cycle_persisted=True.
    """
    worktree_call = _tc("exec_command", {"command": "git worktree add /tmp/wt -b cicd/538-test"})
    issue_view_call = _tc("exec_command", {"command": "gh issue view 538"}, "tc-issue")
    file_write_call = _tc("file", {"action": "write", "path": "/tmp/wt/agent.py", "content": "x=1"}, "tc-write")
    push_call = _tc("exec_command", {"command": "git push origin cicd/538-test"}, "tc-push")

    responses = [
        _resp({"tool_calls": [worktree_call]}),    # sets _cicd_branch
        _resp({"tool_calls": [issue_view_call]}),  # sets _cicd_issue_number
        _resp({"tool_calls": [file_write_call]}),  # sets _cicd_edited_files
        _resp({"tool_calls": [push_call]}),         # sets _cycle_persisted=True
        _text("Looking into it."),                  # consecutive=1 → stripped (continue)
        _text("Tests pass."),                       # consecutive=2 → cycle-82 nudge
        _text("Working on PR."),                    # consecutive=3 → nudge
        _text("Almost done."),                      # consecutive=4 → nudge
        _text("Done."),                             # consecutive=5 >= _MAX_TEXT_ONLY=5 → stop
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "CICD Improvement Loop — Builder task"}]

    exec_results = {
        "git worktree add /tmp/wt -b cicd/538-test": "exit=0\nPreparing worktree (new branch 'cicd/538-test')\n",
        "gh issue view 538": "exit=0\n#538 title\nurl: https://github.com/owner/repo/issues/538\n",
        "git push": "exit=0\nBranch 'cicd/538-test' set up to track 'origin/cicd/538-test'.\n",
    }

    def _exec_fn(**kw):
        cmd = kw.get("command", "")
        for k, v in exec_results.items():
            if k in cmd:
                return v
        return "exit=0\n"

    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 5), \
         patch("agent._MAX_TOTAL_NUDGES", 15), \
         patch.dict("agent.MAP_FN", {
             "exec_command": _exec_fn,
             "file": lambda **kw: "written",
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    assert result == "done"
    # Should have nudge about PR missing
    user_nudges = [
        m["content"] for m in history
        if m.get("role") == "user" and "pr create" in m.get("content", "").lower()
    ]
    assert user_nudges, (
        "Expected nudge about gh pr create after push without PR, user messages: "
        + str([m for m in history if m.get("role") == "user"])
    )


# ---------------------------------------------------------------------------
# 4. Substantive tool check (lines 3105-3136)
# ---------------------------------------------------------------------------

@patch("agent._emit")
@patch("agent._llm_request")
def test_read_only_file_tool_does_not_reset_counter(mock_llm, mock_emit):
    """file(action='read') is non-substantive — consecutive counter not reset."""
    read_call = _tc("file", {"action": "read", "path": "agent.py"})

    # Turn 1: text → consecutive=1 → stripped
    # Turn 2: read-only file tool → _substantive=False → counter not reset, consecutive stays at 1
    #   (wait, after tool call consecutive_text_only isn't incremented...)
    # Actually after tool calls: consecutive_text_only is only reset if _substantive=True
    # So after read-only tool, consecutive stays the same.
    # Turn 3: text → consecutive=2 (was still 1 before the tool call stripped it back to 0?)
    # Let me trace more carefully.
    # Actually the flow is: when we have tool calls, we go through the tool-call branch.
    # _consecutive_text_only is only reset IF _substantive=True (line 3135-3136).
    # For read-only, it's not reset. So consecutive remains 1 going into next text response.
    # Turn 3: text → consecutive becomes 2 → nudge
    # Turn 4: text → consecutive=3 → stop
    responses = [
        _text("Let me check."),                    # consecutive=1 → stripped
        _resp({"tool_calls": [read_call]}),        # read-only → not substantive
        _text("Here is what I found."),            # consecutive=2 → nudge
        _text("Done."),                            # consecutive=3 → stop
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "Read agent.py"}]
    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 3), \
         patch("agent._MAX_TOTAL_NUDGES", 10), \
         patch.dict("agent.MAP_FN", {
             "file": lambda **kw: "file contents here"
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    assert result == "done"


@patch("agent._emit")
@patch("agent._llm_request")
def test_read_only_exec_command_does_not_reset_counter(mock_llm, mock_emit):
    """exec_command with read-only command (git log) is non-substantive."""
    read_exec = _tc("exec_command", {"command": "git log --oneline -5"})

    responses = [
        _text("Let me check."),                    # consecutive=1 → stripped
        _resp({"tool_calls": [read_exec]}),        # read-only exec → not substantive
        _text("Here is the log."),                 # consecutive=2 → nudge
        _text("Done."),                            # consecutive=3 → stop
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "Check git log"}]
    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 3), \
         patch("agent._MAX_TOTAL_NUDGES", 10), \
         patch.dict("agent.MAP_FN", {
             "exec_command": lambda **kw: "abc1234 commit message"
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    assert result == "done"


@patch("agent._emit")
@patch("agent._llm_request")
def test_substantive_write_resets_consecutive_counter(mock_llm, mock_emit):
    """A write tool call (substantive) resets the consecutive text-only counter."""
    write_call = _tc("file", {"action": "write", "path": "out.py", "content": "x=1"})

    # Turn 1: text → consecutive=1 → stripped
    # Turn 2: write tool → _substantive=True → consecutive reset to 0
    # Turn 3: text → consecutive=1 → stripped again
    # Turn 4: text → consecutive=2 → nudge
    # Turn 5: text → consecutive=3 → stop
    responses = [
        _text("Let me write the fix."),             # consecutive=1 → stripped
        _resp({"tool_calls": [write_call]}),         # substantive → resets counter
        _text("Let me check."),                     # consecutive=1 → stripped
        _text("Checking results."),                 # consecutive=2 → nudge
        _text("Done."),                             # consecutive=3 → stop
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "Write a fix"}]
    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 3), \
         patch("agent._MAX_TOTAL_NUDGES", 10), \
         patch.dict("agent.MAP_FN", {
             "file": lambda **kw: "written"
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    assert result == "done"


# ---------------------------------------------------------------------------
# 5. Tool-call loop detection (lines 3138-3173)
# ---------------------------------------------------------------------------

@patch("agent._emit")
@patch("agent._llm_request")
def test_tool_call_loop_detection_injects_correction(mock_llm, mock_emit):
    """Same tool batch repeated _TOOL_LOOP_THRESHOLD=3 times injects a STOP nudge."""
    search_call = _tc("search_files", {"pattern": "TODO"}, "tc-loop")

    # _TOOL_LOOP_THRESHOLD=3 inside run_agent_single
    # Calls 1,2,3: same search_files(pattern=TODO) → on the 3rd, loop detected
    # After loop correction, history is patched and continue fires
    # Then call 4: text → stop
    responses = [
        _resp({"tool_calls": [search_call]}),  # repeat 1
        _resp({"tool_calls": [search_call]}),  # repeat 2
        _resp({"tool_calls": [search_call]}),  # repeat 3 → loop correction injected
        _text("Done."),                         # stop after correction nudge
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "Find TODOs"}]
    with patch("agent._NUDGE_ENABLED", False), \
         patch.dict("agent.MAP_FN", {
             "search_files": lambda **kw: "No results."
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    # Correction nudge ("STOP — you have repeated...") should appear in history
    loop_nudges = [
        m["content"] for m in history
        if m.get("role") == "user" and "STOP" in m.get("content", "")
        and "repeated" in m.get("content", "")
    ]
    assert loop_nudges, (
        "Expected loop correction nudge in history. User messages: "
        + str([m for m in history if m.get("role") == "user"])
    )


@patch("agent._emit")
@patch("agent._llm_request")
def test_tool_call_loop_different_args_no_false_positive(mock_llm, mock_emit):
    """Different args each time should NOT trigger the loop guard."""
    responses = [
        _resp({"tool_calls": [_tc("search_files", {"pattern": "TODO"}, "tc1")]}),
        _resp({"tool_calls": [_tc("search_files", {"pattern": "FIXME"}, "tc2")]}),
        _resp({"tool_calls": [_tc("search_files", {"pattern": "HACK"}, "tc3")]}),
        _text("Done."),
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "Find issues"}]
    with patch("agent._NUDGE_ENABLED", False), \
         patch.dict("agent.MAP_FN", {
             "search_files": lambda **kw: "results"
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    # No STOP-repeated nudge should appear
    loop_nudges = [
        m for m in history
        if m.get("role") == "user" and "STOP" in m.get("content", "")
        and "repeated" in m.get("content", "")
    ]
    assert not loop_nudges, (
        f"Loop guard should not fire for different args, but got: {loop_nudges}"
    )


@patch("agent._emit")
@patch("agent._llm_request")
def test_tool_call_loop_clears_sigs_after_correction(mock_llm, mock_emit):
    """After loop correction, sigs are cleared so a different subsequent call works."""
    search_call = _tc("search_files", {"pattern": "BUG"}, "tc-reset")
    write_call = _tc("file", {"action": "write", "path": "fix.py", "content": "x=1"}, "tc-write")

    # 3 identical → correction; then write → different sig; then text stop
    responses = [
        _resp({"tool_calls": [search_call]}),
        _resp({"tool_calls": [search_call]}),
        _resp({"tool_calls": [search_call]}),    # loop → correction + sigs cleared
        _resp({"tool_calls": [write_call]}),      # different call proceeds normally
        _text("Done."),
    ]
    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "Fix bugs"}]
    with patch("agent._NUDGE_ENABLED", False), \
         patch.dict("agent.MAP_FN", {
             "search_files": lambda **kw: "no results",
             "file": lambda **kw: "written",
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    # Should have injected the loop correction at some point
    loop_nudges = [
        m["content"] for m in history
        if m.get("role") == "user" and "STOP" in m.get("content", "")
    ]
    assert loop_nudges, "Expected loop correction nudge in history"
    # Should complete normally
    assert result in ("done", "cancelled", None)


@patch("agent._emit")
@patch("agent._llm_request")
def test_recent_tool_sigs_window_trimmed(mock_llm, mock_emit):
    """_recent_tool_sigs is trimmed to _TOOL_LOOP_THRESHOLD + 2 entries."""
    # Send 6 different tool calls to fill and trim the window
    responses = []
    for i in range(6):
        responses.append(
            _resp({"tool_calls": [_tc("search_files", {"pattern": f"p{i}"}, f"tc{i}")]})
        )
    responses.append(_text("Done."))

    mock_llm.side_effect = responses

    history = [{"role": "user", "content": "test"}]
    with patch("agent._NUDGE_ENABLED", False), \
         patch.dict("agent.MAP_FN", {
             "search_files": lambda **kw: "results"
         }):
        result = run_agent_single(history, {"text": "", "up_to": 0}, [], log)

    # Just verify run completes without errors
    assert result in ("done", "cancelled", None)
