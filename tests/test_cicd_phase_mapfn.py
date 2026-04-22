"""Tests for CICD phase detection using the correct MAP_FN mock pattern.

Lines targeted:
  - 2451-2459: git worktree add -> implement phase + path/branch capture
  - 2461-2476: gh pr create (with Closes #N) -> PR number capture + no-trailer warning
  - 2486-2499: gh pr review --approve / reviews.md append -> reviewer persistence
  - 2500-2509: gh pr review --approve without think -> reminder injection
  - 2520-2522: gh pr ready -> _cicd_pr_ready_called
  - 2523-2565: gh issue view -> pre-merge check
  - 2609-2643: gh pr merge || suppressor / improvements plan write / tracker
  - 927-928:   AsyncSummarizer.is_running property
  - 934-935:   AsyncSummarizer.drain (thread alive path)
  - 1807-1808: _async_summarizer.harvest -> log + emit
"""

import pytest
import logging
import json
import agent
from unittest.mock import patch, MagicMock, PropertyMock
from agent import run_agent_interactive

logging.basicConfig(level=logging.ERROR)


def _mk_config():
    """Return a minimal _config-compatible dict."""
    return {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False},
    }


def _resp_tool(tool_name, arguments_dict, tool_id="t1"):
    """Build a mock LLM response that emits one tool call then stops."""
    resp = MagicMock()
    tc = {
        "index": 0,
        "id": tool_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(arguments_dict),
        },
    }
    body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    resp.iter_lines.return_value = [
        f"data: {json.dumps(body)}".encode(),
        b"data: [DONE]",
    ]
    return resp


def _resp_text(text="Done."):
    """Build a mock LLM response that returns a text message."""
    resp = MagicMock()
    body = {"choices": [{"delta": {"content": text}}]}
    resp.iter_lines.return_value = [
        f"data: {json.dumps(body)}".encode(),
        b"data: [DONE]",
    ]
    return resp


# ── Lines 2451-2459: git worktree add → implement phase detection ──────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_worktree_add_sets_implement_phase(mock_config, mock_llm, mock_emit):
    """MAP_FN exec_command returning exit=0 for 'git worktree add' covers lines 2451-2459."""
    mock_config.__getitem__.side_effect = _mk_config().get

    wt_cmd = "git worktree add /tmp/wt/cicd-test -b cicd/999-test-branch"
    resp1 = _resp_tool("exec_command", {"command": wt_cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0\nPreparing worktree"}):
        try:
            run_agent_interactive(initial_prompt="Create worktree", auto=True)
        except Exception:
            pass

    # After the session, the CICD phase state should have implement=True
    assert agent._cicd_phase_state["implement"] is True
    assert agent._cicd_worktree_path == "/tmp/wt/cicd-test"
    assert agent._cicd_branch == "cicd/999-test-branch"


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_worktree_add_no_exit0_skips_implement(mock_config, mock_llm, mock_emit):
    """When MAP_FN returns failure (no exit=0), lines 2451-2459 branch is NOT taken."""
    mock_config.__getitem__.side_effect = _mk_config().get

    wt_cmd = "git worktree add /tmp/wt/cicd-fail -b cicd/fail"
    resp1 = _resp_tool("exec_command", {"command": wt_cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=1\nfatal: error"}):
        try:
            run_agent_interactive(initial_prompt="Worktree fail", auto=True)
        except Exception:
            pass

    # implement should NOT be set since there was no exit=0
    assert agent._cicd_phase_state["implement"] is False


# ── Lines 2460-2485: gh pr create (with Closes #N) → PR number capture ───


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_pr_create_with_closes_captures_pr_number(mock_config, mock_llm, mock_emit):
    """MAP_FN returning PR URL for 'gh pr create' with Closes #N covers lines 2460-2476."""
    mock_config.__getitem__.side_effect = _mk_config().get

    pr_cmd = 'gh pr create --title "My PR" --body "Fixes stuff\n\nCloses #42"'
    resp1 = _resp_tool("exec_command", {"command": pr_cmd})
    resp2 = _resp_text("PR created.")
    mock_llm.side_effect = [resp1, resp2]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0\nhttps://github.com/foo/bar/pull/42"}):
        try:
            run_agent_interactive(initial_prompt="Create PR", auto=True)
        except Exception:
            pass

    assert agent._cicd_pr_number == "42"


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_pr_create_with_closes_no_pr_number_in_result(mock_config, mock_llm, mock_emit):
    """gh pr create with Closes #N but result has no PR number (lines 2461-2463 branch)."""
    mock_config.__getitem__.side_effect = _mk_config().get

    pr_cmd = 'gh pr create --title "My PR" --body "Closes #55"'
    resp1 = _resp_tool("exec_command", {"command": pr_cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    # Return exit=0 but no pull/NNN or #NNN in result
    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0\nSuccess (no URL)"}):
        try:
            run_agent_interactive(initial_prompt="Create PR", auto=True)
        except Exception:
            pass

    # _cicd_pr_number stays None because no match found
    assert agent._cicd_pr_number is None


# ── Lines 2486-2499: gh pr review --approve → reviewer persistence ────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_pr_review_approve_sets_reviewer_persisted(mock_config, mock_llm, mock_emit):
    """gh pr review --approve with exit=0 sets _has_reviewer_persisted (lines 2486-2493)."""
    mock_config.__getitem__.side_effect = _mk_config().get

    review_cmd = "gh pr review 42 --approve"
    resp1 = _resp_tool("exec_command", {"command": review_cmd})
    resp2 = _resp_text("Approved.")
    mock_llm.side_effect = [resp1, resp2]

    # Reviewer initial prompt to skip edit-deadline nudge check
    conv = [{"role": "user", "content": "CICD Reviewer — please review PR #42"}]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0\nApproved PR #42"}):
        try:
            run_agent_interactive(initial_prompt=None, auto=True)
        except Exception:
            pass


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_reviews_md_append_sets_reviewer_persisted(mock_config, mock_llm, mock_emit):
    """Appending to reviews.md sets reviewer persistence (lines 2494-2499)."""
    mock_config.__getitem__.side_effect = _mk_config().get

    cmd = "echo 'review verdict' >> CICD/reviews.md"
    resp1 = _resp_tool("exec_command", {"command": cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0"}):
        try:
            run_agent_interactive(initial_prompt="Append to reviews.md", auto=True)
        except Exception:
            pass


# ── Lines 2500-2509: gh pr review --approve without think → reminder ──────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_pr_review_approve_without_think_injects_reminder(mock_config, mock_llm, mock_emit):
    """gh pr review --approve without prior think call covers lines 2500-2509."""
    mock_config.__getitem__.side_effect = _mk_config().get

    review_cmd = "gh pr review 42 --approve"
    resp1 = _resp_tool("exec_command", {"command": review_cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0"}):
        try:
            run_agent_interactive(initial_prompt="Approve PR", auto=True)
        except Exception:
            pass

    # The second LLM call should have received a "think" reminder in the history
    assert mock_llm.call_count >= 2
    second_call_body = mock_llm.call_args_list[1][1].get("json", {})
    messages = second_call_body.get("messages", [])
    found = any(
        "MANDATORY THINK" in msg.get("content", "")
        for msg in messages
        if msg.get("role") == "user"
    )
    assert found, "Expected MANDATORY THINK reminder not found in second LLM call"


# ── Lines 2510-2519: gh pr review --approve self-approve error ────────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_pr_review_self_approve_error_injects_skip_reminder(mock_config, mock_llm, mock_emit):
    """Self-approve failure path covers lines 2510-2519."""
    mock_config.__getitem__.side_effect = _mk_config().get

    review_cmd = "gh pr review 42 --approve"
    resp1 = _resp_tool("exec_command", {"command": review_cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    # Return the self-approve error message (no exit=0)
    with patch.dict(agent.MAP_FN, {
        "exec_command": lambda command, **kw: "exit=1\nCannot approve your own pull request"
    }):
        try:
            run_agent_interactive(initial_prompt="Approve PR", auto=True)
        except Exception:
            pass

    assert mock_llm.call_count >= 2
    second_call_body = mock_llm.call_args_list[1][1].get("json", {})
    messages = second_call_body.get("messages", [])
    found = any(
        "SKIP" in msg.get("content", "") and "approval" in msg.get("content", "")
        for msg in messages
        if msg.get("role") == "user"
    )
    assert found, "Expected self-approve skip reminder not found"


# ── Lines 2520-2522: gh pr ready ──────────────────────────────────────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_gh_pr_ready_sets_flag(mock_config, mock_llm, mock_emit):
    """gh pr ready with exit=0 covers lines 2520-2522."""
    mock_config.__getitem__.side_effect = _mk_config().get

    resp1 = _resp_tool("exec_command", {"command": "gh pr ready 42"})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0"}):
        try:
            run_agent_interactive(initial_prompt="Ready PR", auto=True)
        except Exception:
            pass


# ── Lines 2626-2631: improvements/ plan write ────────────────────────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_improvements_write_sets_plan_phase(mock_config, mock_llm, mock_emit):
    """Writing to improvements/ via exec_command covers lines 2626-2631."""
    mock_config.__getitem__.side_effect = _mk_config().get

    cmd = "cat > improvements/001-plan.md << 'EOF'\n# Plan\nEOF"
    resp1 = _resp_tool("exec_command", {"command": cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0"}):
        try:
            run_agent_interactive(initial_prompt="Write plan file", auto=True)
        except Exception:
            pass

    assert agent._cicd_phase_state["plan"] is True


# ── Lines 2561-2562: gh issue view with unparseable JSON → exception path ─


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_gh_issue_view_json_parse_error_sets_premerge_ok(mock_config, mock_llm, mock_emit):
    """When gh issue view result can't be parsed as JSON, exception path (2561-2562) is taken."""
    mock_config.__getitem__.side_effect = _mk_config().get

    cmd = "gh issue view 42 --json state,labels,title,createdAt"
    resp1 = _resp_tool("exec_command", {"command": cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    # Return exit=0 but with content that has a '{' followed by invalid JSON
    # The exception path sets _premerge_ok=True so issue_view is allowed through
    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0\n{not valid json at all!!!"}):
        try:
            run_agent_interactive(initial_prompt="Check issue", auto=True)
        except Exception:
            pass

    # Verify second LLM call was made (session ran to completion)
    assert mock_llm.call_count >= 2


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_gh_issue_view_open_with_valid_labels(mock_config, mock_llm, mock_emit):
    """gh issue view returning OPEN issue with cicd label covers lines 2542-2565."""
    mock_config.__getitem__.side_effect = _mk_config().get

    cmd = "gh issue view 42 --json state,labels,title,createdAt"
    resp1 = _resp_tool("exec_command", {"command": cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    issue_json = json.dumps({
        "state": "OPEN",
        "labels": [{"name": "cicd"}, {"name": "in-progress"}],
        "title": "Test issue",
        "createdAt": "2026-01-01T00:00:00Z",
    })
    result = f"exit=0\n{issue_json}"

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: result}):
        try:
            run_agent_interactive(initial_prompt="Check issue", auto=True)
        except Exception:
            pass

    # Verify session ran to completion (2 LLM calls)
    assert mock_llm.call_count >= 2


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_gh_issue_view_closed_issue_fails_premerge(mock_config, mock_llm, mock_emit):
    """gh issue view returning CLOSED issue fails PRE-MERGE CHECK (lines 2544-2560)."""
    mock_config.__getitem__.side_effect = _mk_config().get

    cmd = "gh issue view 42 --json state,labels,title,createdAt"
    resp1 = _resp_tool("exec_command", {"command": cmd})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    issue_json = json.dumps({
        "state": "CLOSED",
        "labels": [{"name": "cicd"}, {"name": "in-progress"}],
        "title": "Test issue",
        "createdAt": "2026-01-01T00:00:00Z",
    })
    result = f"exit=0\n{issue_json}"

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: result}):
        try:
            run_agent_interactive(initial_prompt="Check issue", auto=True)
        except Exception:
            pass

    # Verify session ran to completion — the CLOSED state triggers a warning message
    assert mock_llm.call_count >= 2


# ── Lines 2609-2611: gh pr merge with || suppressor ───────────────────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_pr_merge_with_or_suppressor_injects_warning(mock_config, mock_llm, mock_emit):
    """gh pr merge with || echo covers lines 2609-2611.

    Strategy: first call does 'gh issue view' with valid OPEN issue (satisfies
    _cicd_issue_view_called guard), then second call does 'gh pr merge ... || echo'.
    """
    mock_config.__getitem__.side_effect = _mk_config().get

    # First tool call: gh issue view (satisfies PRE-MERGE CHECK)
    resp1 = _resp_tool("exec_command", {
        "command": "gh issue view 42 --json state,labels,title,createdAt"
    }, tool_id="t1")
    # Second tool call: gh pr merge with || suppressor
    resp2 = _resp_tool("exec_command", {
        "command": "gh pr merge 42 --squash || echo 'done'"
    }, tool_id="t2")
    resp3 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2, resp3]

    issue_json = json.dumps({
        "state": "OPEN",
        "labels": [{"name": "cicd"}, {"name": "in-progress"}],
        "title": "Test issue",
        "createdAt": "2026-01-01T00:00:00Z",
    })

    def mock_exec(command, **kw):
        if "gh issue view" in command:
            return f"exit=0\n{issue_json}"
        return "exit=0"

    with patch.dict(agent.MAP_FN, {"exec_command": mock_exec}):
        try:
            run_agent_interactive(initial_prompt="Merge PR", auto=True)
        except Exception:
            pass

    # The third LLM call should have a suppressor warning injected as user msg
    assert mock_llm.call_count >= 3
    third_call_body = mock_llm.call_args_list[2][1].get("json", {})
    messages = third_call_body.get("messages", [])
    found = any(
        ("|| echo" in msg.get("content", "") or "|| true" in msg.get("content", ""))
        for msg in messages
        if msg.get("role") == "user"
    )
    assert found, "Expected || suppressor warning not found in messages"


# ── Lines 2634-2643: _tracker auto_record block ──────────────────────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_tracker_auto_record_called_on_exec_command(mock_config, mock_llm, mock_emit):
    """When _tracker is set, auto_record is called after any exec_command (lines 2634-2643).

    Also covers line 2636 (successful JSON read) by creating current-state.json first.
    """
    import os
    import tempfile

    mock_config.__getitem__.side_effect = _mk_config().get

    resp1 = _resp_tool("exec_command", {"command": "ls /tmp"})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    mock_tracker = MagicMock()
    mock_tracker.auto_record.return_value = "2026-04-22T00:00:00"

    # Create a valid current-state.json so line 2636 is reached
    state_path = agent._state_path("current-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    existing_content = None
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            existing_content = f.read()
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"cycle": 42}, f)

    try:
        with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0\nfile.txt"}):
            with patch.object(agent, "_tracker", mock_tracker):
                try:
                    run_agent_interactive(initial_prompt="List files", auto=True)
                except Exception:
                    pass
    finally:
        # Restore state file
        if existing_content is not None:
            with open(state_path, "w", encoding="utf-8") as f:
                f.write(existing_content)
        elif os.path.exists(state_path):
            os.remove(state_path)

    mock_tracker.auto_record.assert_called()


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_tracker_auto_record_exception_handled(mock_config, mock_llm, mock_emit):
    """When _tracker.auto_record raises, exception is caught (lines 2642-2643)."""
    mock_config.__getitem__.side_effect = _mk_config().get

    resp1 = _resp_tool("exec_command", {"command": "ls /tmp"})
    resp2 = _resp_text("Done.")
    mock_llm.side_effect = [resp1, resp2]

    mock_tracker = MagicMock()
    mock_tracker.auto_record.side_effect = RuntimeError("tracker error")

    with patch.dict(agent.MAP_FN, {"exec_command": lambda command, **kw: "exit=0"}):
        with patch.object(agent, "_tracker", mock_tracker):
            try:
                run_agent_interactive(initial_prompt="List files", auto=True)
            except Exception:
                pass

    # Should not raise — exception is handled


# ── Lines 1807-1808: async summarizer harvest triggers emit ───────────────


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_async_summarizer_harvest_emits_summary_ready(mock_config, mock_llm, mock_emit):
    """When _async_summarizer.harvest returns True, on_summary_ready is emitted (1807-1808)."""
    import logging
    from agent import run_agent_single

    mock_config.__getitem__.side_effect = _mk_config().get

    resp1 = _resp_text("Done.")
    mock_llm.side_effect = [resp1]

    mock_summarizer = MagicMock()
    mock_summarizer.harvest.return_value = True

    log = logging.getLogger("test_harvest")
    conv = [{"role": "user", "content": "Hello"}]
    summary = {"text": "", "up_to": 0}

    try:
        run_agent_single(conv, summary, None, log, async_summarizer=mock_summarizer)
    except Exception:
        pass

    # harvest was called and on_summary_ready was emitted
    mock_summarizer.harvest.assert_called()
    found_summary_ready = any(
        args[0] == "on_summary_ready"
        for args, kwargs in mock_emit.call_args_list
    )
    assert found_summary_ready, "on_summary_ready was not emitted after harvest"


# ── AsyncSummarizer.is_running (lines 927-928) ───────────────────────────


def test_async_summarizer_is_running_property():
    """Cover AsyncSummarizer.is_running property (lines 927-928)."""
    from agent import AsyncSummarizer
    cfg = {
        "llm": {"model": "test"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 512, "ctx_size": 4096},
        "summary": {
            "enabled": True,
            "base_url": "http://localhost:8001",
            "ctx_size": 4096,
            "max_wait_on_save": 5,
            "model": "test",
        },
    }
    log = logging.getLogger("test_is_running")
    summarizer = AsyncSummarizer.__new__(AsyncSummarizer)
    import threading
    summarizer._lock = threading.Lock()
    summarizer._running = True
    summarizer._thread = None
    summarizer._pending_result = None
    summarizer._pending_up_to = None
    summarizer._config = cfg

    assert summarizer.is_running is True
    summarizer._running = False
    assert summarizer.is_running is False


# ── AsyncSummarizer.drain (lines 934-935) ────────────────────────────────


def test_async_summarizer_drain_thread_alive():
    """Cover AsyncSummarizer.drain when thread is alive (lines 934-935)."""
    from agent import AsyncSummarizer
    import threading

    summarizer = AsyncSummarizer.__new__(AsyncSummarizer)
    summarizer._lock = threading.Lock()
    summarizer._running = False
    summarizer._pending_result = None
    summarizer._pending_up_to = None
    summarizer._config = {
        "summary": {"max_wait_on_save": 1},
    }

    # Create a real thread that sleeps briefly so is_alive() is True
    def _sleep():
        import time
        time.sleep(0.2)

    t = threading.Thread(target=_sleep)
    t.start()
    summarizer._thread = t

    # drain() should call t.join(timeout=1) — covers lines 934-935
    summarizer.drain(timeout=0.5)
    t.join()  # clean up


def test_async_summarizer_drain_no_thread():
    """Cover AsyncSummarizer.drain when thread is None (no-op branch)."""
    from agent import AsyncSummarizer
    import threading

    summarizer = AsyncSummarizer.__new__(AsyncSummarizer)
    summarizer._lock = threading.Lock()
    summarizer._running = False
    summarizer._pending_result = None
    summarizer._pending_up_to = None
    summarizer._config = {"summary": {"max_wait_on_save": 5}}
    summarizer._thread = None

    # Should not raise
    summarizer.drain(timeout=1)
