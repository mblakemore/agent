"""Lazy tools: tiers, the catalog line, tool_search, sticky loads applied once per turn,
aliases, implicit loads, preload from the prompt, checkpoint persistence
(beewatcher/agent-tool-search-spec.md, 2026-11-02)."""

import copy
import json
import logging
from unittest.mock import MagicMock

import pytest

import agent
import tools as registry
import tools.tool_search as tool_search

log = logging.getLogger("test_tool_tiers")


def _tool_turn(tid, name, args):
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Tiers back to the shipped defaults and no loads, before and after each test."""
    saved = dict(registry.TIERS)
    registry.reset_session()
    registry.apply_tiers({}, None)
    yield
    registry.TIERS.clear()
    registry.TIERS.update(saved)
    registry.rebuild()
    registry.reset_session()


@pytest.fixture
def loop(monkeypatch, agent_loop_mocks):
    backend = MagicMock(kind="aicommons", model="claude-v5.5-opus", honours_system=True, base_url="https://gw/v1")
    monkeypatch.setattr(agent, "_main_backend", backend)
    monkeypatch.setattr(agent, "_END_CYCLE_ENABLED", False)
    sent = []
    real = agent_loop_mocks._fake_llm_request

    def capture(log, **kw):
        sent.append(copy.deepcopy(kw["json"]))
        return real(log, **kw)
    monkeypatch.setattr(agent, "_llm_request", capture)
    agent.MAP_FN["tool_search"] = tool_search.fn
    return agent_loop_mocks, sent


def _names(body):
    return [t["function"]["name"] for t in body.get("tools", [])]


def _catalog(body):
    system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
    return next((ln for ln in system.split("\n") if ln.startswith(registry.CATALOG_PREFIX)), "")


# ── registry ──────────────────────────────────────────────────────────

def test_shipped_tiers():
    assert set(registry.names("core")) == set(registry.DEFAULT_CORE)
    assert "read_pdf" in registry.names("deferred")
    assert "end_cycle" in registry.names("hidden")
    assert [t["function"]["name"] for t in registry.tools] == registry.names("core")


def test_config_moves_tools_between_tiers_and_warns_on_nonsense(caplog):
    with caplog.at_level(logging.WARNING, logger="agent"):
        counts = registry.apply_tiers({"core": ["monitor"], "deferred": ["list_files", "monitor"], "hidden": ["nope"]})
    assert registry.TIERS["monitor"] == "core" and registry.TIERS["list_files"] == "deferred"
    assert "monitor is already in tools.core" in caplog.text and "unknown tool: nope" in caplog.text
    assert counts["core"] == len(registry.DEFAULT_CORE)  # +monitor -list_files


def test_a_whitelist_alone_keeps_its_old_meaning():
    registry.apply_tiers({}, whitelist=["exec_command", "read_pdf", "sleep"])
    assert set(registry.DEFINITIONS) == {"exec_command", "read_pdf", "sleep"}
    assert registry.names("core") == ["exec_command", "read_pdf", "sleep"]
    assert registry.search("pdf") == ["read_pdf"]
    registry.TIERS.clear()
    registry.DEFINITIONS.clear()
    registry.META.clear()
    registry._discover_tools()  # the other tests want the full registry back


def test_the_catalog_line_names_the_deferred_tools_in_a_stable_order():
    line = registry.catalog_line()
    assert line.startswith(registry.CATALOG_PREFIX)
    for n in registry.names("deferred"):
        assert f"`{n}`" in line
    assert "`exec_command`" not in line and "`end_cycle`" not in line
    assert line == registry.catalog_line()


def test_a_closed_gate_hides_a_tool_from_search_and_catalog():
    registry.GATES["read_pdf"] = lambda: False
    try:
        assert not registry.available("read_pdf")
        assert "read_pdf" not in registry.search("pdf")
        assert "`read_pdf`" not in registry.catalog_line()
        assert registry.request_load("read_pdf") is False
    finally:
        registry.GATES.pop("read_pdf")


def test_search_scores_names_and_aliases_above_descriptions():
    assert registry.search("pdf", 1) == ["read_pdf"]
    assert registry.search("bash", 1) == ["exec_command"]
    assert registry.search("screenshot page", 1) == ["browser_check"]
    assert registry.search("zzzz qqqq") == []
    assert registry.resolve("grep") == "search_files" and registry.resolve("Read") == "read_file"
    assert registry.resolve("nothing_here") is None


def test_args_fit():
    assert registry.args_fit("read_pdf", {"path": "a.pdf"})
    assert not registry.args_fit("read_pdf", {"file": "a.pdf"})
    assert not registry.args_fit("read_pdf", {})
    assert not registry.args_fit("read_pdf", "a.pdf")


def test_preload_from_text_takes_names_aliases_and_clear_matches():
    assert registry.preload_from_text("read the pdf of the book, then check the page with a browser screenshot") == ["browser_check", "read_pdf"]
    assert registry.PENDING_LOADS == ["browser_check", "read_pdf"]
    registry.reset_session()
    assert registry.preload_from_text("fix the bug in agent.py") == []
    # a bare common word is not a request for the tool of that name
    assert registry.preload_from_text("open the file and set a goal; think about it") == []
    # the blocks the agent appends to the task name every tool; they do not count
    inlined = ("@CLAUDE.md run your next cycle!\n\n[SYSTEM CONTEXT: Your working directory is /x.]"
               "\n\n[AGENT IDENTITY FILE: CLAUDE.md]\n## Capabilities\n`read_pdf`, `browser_check`, "
               "`find_symbol`, `goal`, `append_file`, `check_file`, `schedule`")
    assert registry.preload_from_text(inlined) == []
    assert registry.own_text(inlined) == "@CLAUDE.md run your next cycle!"
    assert registry.preload_from_text("use find_symbol to map agent.py") == ["find_symbol"]


# ── the tool ──────────────────────────────────────────────────────────

def test_tool_search_loads_matches_once_and_reports_core_tools_as_available():
    out = tool_search.fn("pdf")
    assert "read_pdf(path" in out and "Loaded 1 tool" in out
    assert registry.PENDING_LOADS == ["read_pdf"]
    again = tool_search.fn("select:read_pdf,exec_command,nope")
    assert "exec_command(" in again and "(already available)" in again
    assert "Unknown: nope" in again
    assert registry.PENDING_LOADS == ["read_pdf"]  # not twice
    assert "No tool matches" in tool_search.fn("zzzz qqqq")
    assert "Error" in tool_search.fn("")


# ── the loop ──────────────────────────────────────────────────────────

def test_the_request_carries_core_tools_and_the_catalog_line(loop):
    mocks, sent = loop
    mocks.script()
    agent.run_agent_single([{"role": "user", "content": "say hi"}], {"text": "", "up_to": 0}, [], log)
    assert sent and set(_names(sent[0])) == set(registry.DEFAULT_CORE)
    assert "`read_pdf`" in _catalog(sent[0])


def test_a_search_adds_the_tool_once_for_the_next_request_and_it_stays(loop):
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "tool_search", '{"query": "pdf"}'),
                 _tool_turn("t2", "read_file", '{"path": "README.md"}'))
    history = [{"role": "user", "content": "do the task"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert len(sent) == 3
    assert "read_pdf" not in _names(sent[0])
    assert _names(sent[1]) == _names(sent[0]) + ["read_pdf"]
    assert _names(sent[2]) == _names(sent[1])  # sticky, and only one list change
    assert registry.LOADED == ["read_pdf"]
    result = next(m for m in history if m.get("role") == "tool" and m.get("name") == "tool_search")
    assert "Loaded 1 tool" in result["content"]


def test_two_loads_in_one_turn_change_the_list_once(loop):
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "tool_search", '{"query": "select:read_pdf,browser_check"}'))
    agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], log)
    assert _names(sent[1]) == _names(sent[0]) + ["read_pdf", "browser_check"]


def test_an_alias_resolves_and_the_result_says_so(loop):
    mocks, sent = loop
    ran = []
    agent.MAP_FN["exec_command"] = lambda **kw: ran.append(kw) or "exit=0"
    mocks.script(_tool_turn("t1", "bash", '{"command": "ls"}'))
    history = [{"role": "user", "content": "go"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert ran == [{"command": "ls"}]
    result = next(m for m in history if m.get("role") == "tool" and m.get("name") == "exec_command")
    assert result["content"].startswith("(bash is exec_command here; use that name.) exit=0")


def test_an_unknown_name_gets_a_suggestion_and_loads_nothing(loop):
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "run_pdf_magic", '{"path": "a.pdf"}'))
    history = [{"role": "user", "content": "go"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    result = next(m for m in history if m.get("role") == "tool" and m.get("name") == "run_pdf_magic")
    assert result["content"].startswith("Error: Unknown tool 'run_pdf_magic'.")
    assert "Closest: read_pdf" in result["content"]
    assert registry.LOADED == [] and _names(sent[1]) == _names(sent[0])


def test_a_deferred_tool_called_with_fitting_arguments_runs_and_is_loaded(loop):
    mocks, sent = loop
    ran = []
    agent.MAP_FN["read_pdf"] = lambda **kw: ran.append(kw) or "page text"
    mocks.script(_tool_turn("t1", "read_pdf", '{"path": "a.pdf"}'))
    history = [{"role": "user", "content": "go"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert ran == [{"path": "a.pdf"}]
    assert "read_pdf" in _names(sent[1]) and registry.LOADED == ["read_pdf"]


def test_a_deferred_tool_called_with_wrong_arguments_gets_its_schema_and_is_loaded(loop):
    mocks, sent = loop
    ran = []
    agent.MAP_FN["read_pdf"] = lambda **kw: ran.append(kw) or "page text"
    mocks.script(_tool_turn("t1", "read_pdf", '{"file": "a.pdf"}'))
    history = [{"role": "user", "content": "go"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert ran == []
    result = next(m for m in history if m.get("role") == "tool" and m.get("name") == "read_pdf")
    assert "is not loaded and these arguments do not fit it" in result["content"]
    assert '"name": "read_pdf"' in result["content"]
    assert "read_pdf" in _names(sent[1])


def test_implicit_run_off_answers_with_the_schema_even_when_arguments_fit(loop, monkeypatch):
    monkeypatch.setitem(agent._TOOLS_CFG, "implicit_run", False)
    mocks, sent = loop
    ran = []
    agent.MAP_FN["read_pdf"] = lambda **kw: ran.append(kw) or "page text"
    mocks.script(_tool_turn("t1", "read_pdf", '{"path": "a.pdf"}'))
    history = [{"role": "user", "content": "go"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert ran == [] and "read_pdf" in _names(sent[1])


def test_the_prompt_preloads_named_tools_before_the_first_request(loop):
    mocks, sent = loop
    mocks.script()
    agent.run_agent_single([{"role": "user", "content": "use read_pdf on docs/a.pdf and summarise it"}],
                           {"text": "", "up_to": 0}, [], log)
    assert _names(sent[0])[-1] == "read_pdf"
    assert registry.LOADED == ["read_pdf"]


def test_the_checkpoint_keeps_loaded_tools_and_a_resume_restores_them(loop, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(tmp_path / "cp.json"))
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "tool_search", '{"query": "pdf"}'))
    agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], log)
    saved = json.load(open(tmp_path / "cp.json"))
    assert saved["loaded_tools"] == ["read_pdf"]
    registry.reset_session()
    assert agent._load_checkpoint() is not None
    assert registry.RESUME_LOADED == ["read_pdf"]
    sent.clear()
    mocks.script()
    agent.run_agent_single([{"role": "user", "content": "carry on"}], {"text": "", "up_to": 0}, [], log)
    assert "read_pdf" in _names(sent[0])


def test_the_catalog_can_be_turned_off(loop, monkeypatch):
    monkeypatch.setitem(agent._TOOLS_CFG, "catalog", False)
    mocks, sent = loop
    mocks.script()
    agent.run_agent_single([{"role": "user", "content": "hi"}], {"text": "", "up_to": 0}, [], log)
    assert _catalog(sent[0]) == ""
