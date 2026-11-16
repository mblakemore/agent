"""cicd_gates.py owns the CICD phase state; agent.py reads and assigns it as _cicd.NAME."""
import agent
import cicd_gates
import summarize


def test_aliases_are_the_module_objects():
    assert agent._validate_tool_call is cicd_gates.validate_tool_call
    assert agent._cicd_verify_gh_mutation is cicd_gates.verify_gh_mutation
    assert agent._cmd_has is cicd_gates.cmd_has
    assert agent._handle_cicd_file_edit is cicd_gates.handle_file_edit
    assert agent._cicd_checkpoint_text is cicd_gates.checkpoint_text


def test_reset_starts_a_cycle_with_nothing_learned(monkeypatch):
    monkeypatch.setattr(cicd_gates, "pr_number", 7)
    monkeypatch.setattr(cicd_gates, "edited_files", {"a.py"})
    monkeypatch.setattr(cicd_gates, "phase_state", {"track": True})
    cicd_gates.reset()
    try:
        assert cicd_gates.pr_number is None and cicd_gates.edited_files == set()
        assert set(cicd_gates.phase_state) == set(cicd_gates.PHASES)
        assert not any(cicd_gates.phase_state.values())
        assert cicd_gates.checkpoint_text() == ""
    finally:
        cicd_gates.reset()


def test_state_set_by_the_loop_reaches_the_summary_prompt(monkeypatch):
    monkeypatch.setattr(cicd_gates, "branch", "cicd/x")
    monkeypatch.setattr(cicd_gates, "issue_number", "12")
    monkeypatch.setattr(cicd_gates, "phase_state", {"plan": True})
    assert cicd_gates.facts()[4] == "cicd/x"
    assert "Issue: #12" in cicd_gates.checkpoint_text()
    assert "Branch: cicd/x" in summarize.build_summary_prompt("", [{"role": "user", "content": "hi"}])


def test_cmd_has_ignores_heredoc_bodies_and_quotes():
    assert cicd_gates.cmd_has("cd x && git push -u origin b", "git push")
    assert not cicd_gates.cmd_has("cat > f << 'EOF'\ngit push origin main\nEOF", "git push")
    assert not cicd_gates.cmd_has('echo "git push"', "git push")
