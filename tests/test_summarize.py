"""summarize.py: the module owns the summary knobs and the idle-watcher state, agent.py
keeps the old names as aliases, and the backends come in through DEPS at call time."""
import pytest

import agent
import summarize


def test_aliases_are_the_module_objects():
    assert agent._condense_summary is summarize.condense_summary
    assert agent._generate_summary is summarize.generate_summary
    assert agent._compress_repeated_tool_results is summarize.compress_repeated_tool_results
    assert agent._fold_summary_at_head_move is summarize.fold_summary_at_head_move
    assert agent.AsyncSummarizer is summarize.AsyncSummarizer
    assert agent._SUMMARY_IDLE is summarize.SUMMARY_IDLE


def test_configure_rejects_an_unknown_dep():
    with pytest.raises(TypeError):
        summarize.configure(nope=lambda: 1)


def test_configure_sets_the_knobs(monkeypatch):
    monkeypatch.setattr(summarize, "SUMMARY_THRESHOLD", 5)
    monkeypatch.setattr(summarize, "SUMMARY_MAX_CHARS", 4000)
    summarize.configure(threshold=9, max_chars=123)
    assert (summarize.SUMMARY_THRESHOLD, summarize.SUMMARY_MAX_CHARS) == (9, 123)


def test_summary_backend_is_read_at_call_time(monkeypatch):
    """A patch (or a failover) on agent's backend is what the next request uses."""
    class _B:
        kind = "fake"
        def complete(self, prompt):
            return "S:" + prompt
    monkeypatch.setattr(agent, "_summary_backend", _B())
    assert summarize.summary_request("p") == "S:p"


def test_cicd_facts_come_from_agent(monkeypatch):
    monkeypatch.setattr(agent, "_cicd_branch", "feat/x")
    monkeypatch.setattr(agent, "_cicd_pr_number", 42)
    prompt = summarize.build_summary_prompt("", [{"role": "user", "content": "hi"}])
    assert "Branch: feat/x" in prompt and "PR: #42" in prompt
