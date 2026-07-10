"""WS8 (wave-1) regression tests: guard anchoring + builder self-merge.

Covers:
  - WS8.1 _strip_noncommand_text / _cmd_has: heredoc bodies and quoted
    literals no longer false-trigger command detection (beewatcher runs
    092+, 114), while real compound/substituted commands still match.
  - WS8.1 python-skip tightening: `python3 -c '...' && gh pr merge` no
    longer bypasses every guard (old blanket skip).
  - WS8.2 builder self-merge hard block (run 110).
  - WS8.3 _is_track_scope_call classification.
"""

import logging

import pytest

import agent


log = logging.getLogger("test_ws8")


# ---------------------------------------------------------------- WS8.1 core

class TestStripNoncommandText:
    def test_heredoc_body_removed(self):
        cmd = "cat << 'EOF' > doc.md\nrun git worktree add /tmp/x first\nEOF"
        out = agent._strip_noncommand_text(cmd)
        assert "worktree" not in out

    def test_unterminated_heredoc_removed(self):
        cmd = "cat << EOF > doc.md\n&& git push origin main"
        out = agent._strip_noncommand_text(cmd)
        assert "git push" not in out

    def test_single_quotes_removed(self):
        out = agent._strip_noncommand_text("echo 'gh pr merge 5'")
        assert "merge" not in out

    def test_double_quotes_removed(self):
        out = agent._strip_noncommand_text('echo "gh pr merge 5"')
        assert "merge" not in out

    def test_line_continuation_collapsed(self):
        out = agent._strip_noncommand_text("git \\\ncommit -m x")
        assert "\\\n" not in out

    def test_plain_command_untouched(self):
        assert "git commit" in agent._strip_noncommand_text("git commit -m 'x'")


class TestCmdHas:
    @pytest.mark.parametrize("cmd", [
        "git worktree add /tmp/wt -b cicd/x",
        "cd /repo && git worktree add /tmp/wt",
        "true; git worktree add /tmp/wt",
        "false || git worktree add /tmp/wt",
        "echo hi\ngit worktree add /tmp/wt",
        "echo $(git worktree add /tmp/wt)",
    ])
    def test_real_command_positions_match(self, cmd):
        assert agent._cmd_has(cmd, "git worktree add")

    @pytest.mark.parametrize("cmd", [
        # The run-114 class: keyword inside a heredoc body.
        "cat << 'EOF' > notes.md\nfirst run git worktree add /tmp/x\nEOF",
        # Keyword inside quoted literals.
        "echo 'git worktree add /tmp/x'",
        'echo "use git worktree add later"',
        # Mid-word / argument position, not a command.
        "echo describe-git worktree add",
    ])
    def test_literal_text_does_not_match(self, cmd):
        assert not agent._cmd_has(cmd, "git worktree add")

    def test_git_push_in_heredoc_does_not_fake_persist(self):
        cmd = "cat << EOF >> journal.md\nthen we ran && git push\nEOF"
        assert not agent._cmd_has(cmd, "git push")
        assert agent._cmd_has("git add x && git push origin HEAD", "git push")


# ------------------------------------------------- WS8.1/8.2 validate guards

def _validate(cmd, builder=False, reviewer=False, issue_view=False):
    return agent._validate_tool_call(
        "exec_command", {"command": cmd}, issue_view, log,
        is_cicd_builder=builder, is_cicd_reviewer=reviewer,
    )


class TestValidateToolCall:
    def test_builder_self_merge_blocked_even_after_issue_view(self):
        blocked, msg = _validate("gh pr merge 5 --squash", builder=True,
                                 issue_view=True)
        assert blocked
        assert "BUILDER" in msg

    def test_reviewer_merge_requires_issue_view(self):
        blocked, msg = _validate("gh pr merge 5 --squash", reviewer=True)
        assert blocked
        assert "PRE-MERGE" in msg

    def test_reviewer_merge_allowed_after_issue_view(self):
        blocked, _ = _validate("gh pr merge 5 --squash", reviewer=True,
                               issue_view=True)
        assert not blocked

    def test_merge_keyword_in_heredoc_not_blocked(self):
        cmd = "cat << 'EOF' > notes.md\nnext step: gh pr merge 5\nEOF"
        blocked, _ = _validate(cmd, reviewer=True)
        assert not blocked

    def test_python_dash_c_keywords_still_skipped(self):
        # Cycle 96 intent preserved: pure python invocation, keywords only
        # inside the -c string literal.
        blocked, _ = _validate(
            "python3 -c 'print(\"gh pr merge 5\")'", reviewer=True)
        assert not blocked

    def test_python_compound_bypass_closed(self):
        # Old blanket skip let this through unguarded.
        blocked, msg = _validate(
            "python3 -c 'x=1' && gh pr merge 5 --squash", builder=True)
        assert blocked
        assert "BUILDER" in msg

    def test_push_main_blocked_but_not_in_quotes(self):
        blocked, _ = _validate("git push origin main", builder=True)
        assert blocked
        blocked, _ = _validate('echo "git push origin main"', builder=True)
        assert not blocked


# ---------------------------------------------------------------- WS8.3

class TestTrackScope:
    @pytest.mark.parametrize("name,args", [
        ("end_cycle", {}),
        ("task_tracker", {"action": "done", "description": "TRACK"}),
        ("file", {"action": "append", "path": "CICD/progress-1.md"}),
        ("file", {"action": "write", "path": "improvements/0101-x.results.md"}),
        ("exec_command", {"command": "gh issue comment 5 --body done"}),
        ("exec_command", {"command": "git worktree remove /tmp/wt --force"}),
    ])
    def test_track_scope_allowed(self, name, args):
        assert agent._is_track_scope_call(name, args)

    @pytest.mark.parametrize("name,args", [
        ("search_files", {"pattern": "def foo"}),
        ("file", {"action": "read", "path": "tools/exec_command.py"}),
        ("exec_command", {"command": "pytest tests/ -q"}),
        ("web_fetch", {"url": "https://example.com"}),
    ])
    def test_new_cycle_work_not_track_scope(self, name, args):
        assert not agent._is_track_scope_call(name, args)


# ------------------------------------------------ reviewer draft path (loop)

import json
from unittest.mock import MagicMock, patch

from tools import MAP_FN


def _sse_response(lines):
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = [l.encode() for l in lines]
    resp.close = MagicMock()
    return resp


def _tool_call_resp(command, call_id="t1"):
    payload = {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": call_id,
        "function": {"name": "exec_command",
                     "arguments": json.dumps({"command": command})}}]}}]}
    return _sse_response([f"data: {json.dumps(payload)}", "data: [DONE]"])


def _text_resp(content):
    payload = {"choices": [{"delta": {"content": content}}]}
    return _sse_response([f"data: {json.dumps(payload)}", "data: [DONE]"])


class TestReviewerDraftWarningStillFires:
    """WS8.2 made the builder draft-merge path unreachable (blocked
    pre-execute), so the draft warning is reviewer-only now. This preserves
    coverage of that path (previously in test_cicd_guards param 'PR merge
    on draft')."""

    def test_reviewer_draft_merge_warning(self, monkeypatch):
        issue_json = json.dumps(
            {"state": "OPEN",
             "labels": [{"name": "cicd"}, {"name": "in-progress"}]})
        mock_exec = MagicMock(side_effect=[
            f"exit=0\n{issue_json}",
            "exit=1\nError: PR is still a draft",
        ])
        monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)
        history = [{"role": "user", "content":
                    "# CICD Improvement Loop — Reviewer\n"
                    "I am the CICD Reviewer.\nTest prompt"}]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = [
                _tool_call_resp("gh issue view 1 --json state,labels"),
                _tool_call_resp("gh pr merge 456 --squash", call_id="t2"),
                _text_resp("Done"),
            ]
            agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        hist = "".join(str(m) for m in history)
        assert "The PR is still a draft. You must run" in hist
