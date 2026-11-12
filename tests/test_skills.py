"""Skills: SKILL.md discovery, the catalog line, loading by the model (tool_search
skill:NAME), the keyboard (/skill) and the picker on the summary slot; the loaded set
follows the window; the checkpoint and the summarizer know (beewatcher/agent-skills-plan.md,
2026-11-11).

A scripted judge stands in for the summary model. The guarantees: a skill is text in a
message (no tool list change), a load past the cap is refused with a notice, a skill
trimmed out of the window can be loaded again, and any judge failure loads nothing.
"""

import copy
import json
import logging
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent
import commands
import skills
import tools as registry
import tools.tool_search as tool_search

log = logging.getLogger("test_skills")

ZOOM = """---
name: zoom
description: Joining a meeting with the desk client. Use when asked to join or leave a call.
keywords:
  - zoom
  - "join a meeting"
---
# Zoom
Open the desk, press join.
Say hello.
"""

RELEASE = """---
description: Cutting a release: tag, build, push
allowed-tools: Bash
---
Tag, build, push.
"""

SECRET = """---
name: secret
description: Only the keyboard loads this one
disable-model-invocation: true
---
Hidden instructions.
"""


def _tool_turn(tid, name, args):
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


def _text_turn(text="done"):
    return [{"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A working directory with a Claude Code skill, an .agent skill, a hidden one and a
    registered document; discovery run against it."""
    (tmp_path / ".claude" / "skills" / "zoom").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "zoom" / "SKILL.md").write_text(ZOOM)
    (tmp_path / ".claude" / "skills" / "zoom" / "join.py").write_text("print('hi')\n")
    (tmp_path / ".agent" / "skills" / "release").mkdir(parents=True)
    (tmp_path / ".agent" / "skills" / "release" / "SKILL.md").write_text(RELEASE)
    (tmp_path / ".agent" / "skills" / "secret").mkdir(parents=True)
    (tmp_path / ".agent" / "skills" / "secret" / "SKILL.md").write_text(SECRET)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "style.md").write_text("# Style\nShort sentences.\n")
    monkeypatch.chdir(tmp_path)
    skills.configure({"catalog": [{"name": "style", "path": "docs/style.md",
                                   "description": "The house style for written work",
                                   "keywords": ["house style"]}]},
                     cwd=str(tmp_path), complete=None)
    skills.discover(cwd=str(tmp_path), log=log)
    yield tmp_path
    skills.configure({}, cwd=os.getcwd(), complete=agent._skill_judge)


class Judge:
    """Scripted answers; records every prompt."""

    def __init__(self, *answers):
        self.script = list(answers)
        self.prompts = []
        self.block = None

    def __call__(self, prompt, timeout):
        self.prompts.append(prompt)
        if self.block is not None:
            self.block.wait(5)
        item = self.script.pop(0) if self.script else "[]"
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def loop(monkeypatch, agent_loop_mocks):
    backend = MagicMock(kind="aicommons", model="m", honours_system=True, base_url="https://gw/v1")
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


def _system(body):
    return next((m["content"] for m in body["messages"] if m["role"] == "system"), "")


# ── discovery and the catalog ─────────────────────────────────────────

def test_discovery_reads_both_layouts_and_registered_documents(tree):
    assert list(skills.SKILLS) == ["release", "secret", "zoom", "style"]
    z = skills.SKILLS["zoom"]
    assert z.description.startswith("Joining a meeting") and z.keywords == ["zoom", "join a meeting"]
    assert skills.SKILLS["release"].name == "release"      # the directory name
    assert skills.SKILLS["secret"].hidden and not z.hidden
    assert skills.SKILLS["style"].registered and skills.SKILLS["style"].keywords == ["house style"]


def test_the_catalog_line_is_static_and_skips_hidden_skills(tree):
    line = skills.catalog_line()
    assert line.startswith(skills.CATALOG_PREFIX)
    assert "`zoom` (joining a meeting with the desk client)" in line
    assert "`release` (cutting a release)" in line and "`style`" in line
    assert "secret" not in line
    assert line == skills.catalog_line()


def test_render_carries_a_header_with_the_directory_and_no_frontmatter(tree):
    text = skills.render("zoom")
    assert text.startswith("[skill: zoom (.claude/skills/zoom/SKILL.md), 3 lines — files beside it "
                           "resolve against .claude/skills/zoom]\n# Zoom\n")
    assert "---" not in text and "keywords" not in text
    assert skills.render("style").startswith("[skill: style (docs/style.md), 2 lines]\n# Style")
    assert skills.render("nope") is None


def test_frontmatter_parsing_handles_quotes_lists_and_missing_blocks():
    fields, body = skills.parse_frontmatter('---\nname: "a b"\nkeywords: [x, "y z"]\nextra: 1\n---\nbody\n')
    assert fields == {"name": "a b", "keywords": ["x", "y z"], "extra": "1"} and body == "body\n"
    assert skills.parse_frontmatter("no front\n") == ({}, "no front\n")
    assert skills.parse_frontmatter("---\nnot closed\n") == ({}, "---\nnot closed\n")


def test_a_skill_without_a_description_or_over_the_cap_is_skipped(tmp_path, caplog):
    (tmp_path / ".agent" / "skills" / "bare").mkdir(parents=True)
    (tmp_path / ".agent" / "skills" / "bare" / "SKILL.md").write_text("no frontmatter\n")
    (tmp_path / ".agent" / "skills" / "big").mkdir(parents=True)
    (tmp_path / ".agent" / "skills" / "big" / "SKILL.md").write_text(
        "---\ndescription: too big\n---\n" + "x" * 30000)
    skills.configure({}, cwd=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="agent"):
        assert skills.discover(cwd=str(tmp_path), log=logging.getLogger("agent")) == []
    assert "has no description" in caplog.text and "over skills.max_chars" in caplog.text


def test_a_duplicate_name_keeps_the_first_and_warns(tmp_path, caplog):
    for root in (".agent", ".claude"):
        (tmp_path / root / "skills" / "zoom").mkdir(parents=True)
        (tmp_path / root / "skills" / "zoom" / "SKILL.md").write_text(f"---\ndescription: from {root}\n---\nx\n")
    skills.configure({}, cwd=str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="agent"):
        skills.discover(cwd=str(tmp_path), log=logging.getLogger("agent"))
    assert skills.SKILLS["zoom"].description == "from .agent"
    assert "is already a skill" in caplog.text


def test_config_defaults_and_bad_values(caplog):
    with caplog.at_level(logging.WARNING, logger="agent"):
        cfg = skills.configure({"pick": "sometimes", "max_chars": "x", "load_threshold": "0.9"})
    assert cfg["pick"] == "off" and cfg["max_chars"] == 20000 and cfg["load_threshold"] == 0.9
    assert "unknown level" in caplog.text
    assert skills.configure({"pick": True})["pick"] == "always"
    assert set(skills.configure({})) == set(skills.DEFAULTS)


# ── loading: the model, the keyboard, the window ──────────────────────

def test_tool_search_skill_answers_with_the_text_once_and_lists_the_catalog_for_unknowns(tree):
    out = tool_search.fn("skill:zoom")
    assert out.startswith("[skill: zoom (") and "press join" in out
    assert skills.LOADED == ["zoom"]
    assert "already loaded" in tool_search.fn("skill:zoom")
    assert skills.LOADED == ["zoom"]
    out = tool_search.fn("skill:nope")
    assert "no skill named nope" in out and "Skills available: release, zoom, style" in out
    out = tool_search.fn("skill:secret")                # hidden: the keyboard only
    assert "not available to the model" in out and "Hidden instructions" not in out
    assert skills.request_load("secret", by="command")[0]
    assert registry.PENDING_LOADS == []                 # no tool list change


def test_a_multi_skill_result_stays_loaded_after_a_window_sync(tree):
    first = tool_search.fn("skill:zoom")
    out = tool_search.fn("skill:zoom,release,style")   # a notice first, then two texts
    assert out.startswith("skill zoom is already loaded") and out.count("[skill: ") == 2
    hist = [{"role": "tool", "name": "tool_search", "content": first},
            {"role": "tool", "name": "tool_search", "content": out}]
    assert skills.sync_loaded(hist, 0) == [] and skills.LOADED == ["zoom", "release", "style"]
    assert skills.sync_loaded(hist, 1) == ["zoom"] and skills.LOADED == ["release", "style"]
    # prose that quotes a header is not a load
    prose = [{"role": "tool", "name": "read_file", "content": "log:\n[skill: zoom (x), 1 line]\n"}]
    assert skills.skills_in(prose) == []


def test_max_chars_never_exceeds_the_spill_cap(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="agent"):
        cfg = skills.configure({"max_chars": 30000}, cwd=str(tmp_path), spill_cap=8000)
    assert cfg["max_chars"] == 8000 and "must never spill" in caplog.text
    assert skills.configure({"max_chars": 5000}, cwd=str(tmp_path), spill_cap=8000)["max_chars"] == 5000
    assert skills.configure({}, cwd=str(tmp_path), spill_cap=0)["max_chars"] == 20000   # spill off


def test_a_queued_load_waits_while_a_tool_call_is_unanswered(tree):
    skills.request_load("zoom", by="command")
    hist = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "x", "arguments": "{}"}}]}]
    assert agent._skills_at_boundary(hist, None, 0, log) == ""
    assert len(hist) == 2 and [n for n, _ in skills.PENDING] == ["zoom"] and skills.LOADED == []
    hist.append({"role": "tool", "tool_call_id": "t1", "name": "x", "content": "ok"})
    agent._skills_at_boundary(hist, None, 0, log)
    assert skills.LOADED == ["zoom"] and hist[-1]["_skills"] == ["zoom"]


def test_a_verdict_arriving_after_a_reset_is_dropped(tree):
    skills.CFG["pick"] = "always"
    judge = Judge('[{"name": "zoom", "confidence": 0.9}]')
    judge.block = threading.Event()
    skills.set_judge(judge)
    assert skills.PICKER.kick("join a meeting", "user")
    skills.reset_session()                       # /clear
    judge.block.set()
    time.sleep(0.2)
    assert skills.PICKER.harvest() is None and skills.PENDING == []


def test_the_judge_gets_the_configured_timeout(tree):
    seen = []
    skills.CFG["pick_timeout_s"] = 3.5
    skills.pick("join", complete=lambda p, t: seen.append(t) or "[]")
    assert seen == [3.5]


def test_a_resume_finds_a_skill_a_boundary_appended(tree, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(tmp_path / "cp.json"))
    hist = [{"role": "user", "content": "go"}]
    skills.request_load("zoom", by="command")
    agent._skills_at_boundary(hist, None, 0, log)
    agent._save_checkpoint(hist, {"text": "", "up_to": 0}, 1, None)
    skills.reset_session()
    cp = agent._load_checkpoint()
    assert cp and skills.LOADED == ["zoom"]
    assert skills.sync_loaded(cp[0], 0) == [] and skills.LOADED == ["zoom"]   # the _skills key survived


def test_a_deployment_without_skills_sees_no_change(loop, monkeypatch):
    assert skills.SKILLS == {}
    calls = []
    monkeypatch.setattr(skills, "_complete", lambda p, t: calls.append(p) or "[]")
    skills.CFG["pick"] = "always"
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "read_file", '{"path": "x"}'), _text_turn())
    mocks.stub_tool("read_file", "contents")
    history = [{"role": "user", "content": "do it"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert calls == []
    assert not any("_skill_pick" in m or "_skills" in m for m in history)
    assert all(skills.CATALOG_PREFIX not in _system(b) and skills.INSTRUCTIONS not in _system(b) for b in sent)
    user_text = "".join(m["content"] for m in history if m["role"] == "user")
    assert "[skill:" not in user_text and "A skill may apply" not in user_text
    skills.CFG["pick"] = "off"


def test_tool_search_loads_several_and_refuses_past_the_token_cap(tree):
    out = tool_search.fn("skill:release,style")
    assert out.count("[skill: ") == 2 and skills.LOADED == ["release", "style"]
    skills.CFG["max_loaded_tokens"] = skills.loaded_tokens() + 1
    out = tool_search.fn("skill:zoom")
    assert "would pass skills.max_loaded_tokens" in out and "release, style" in out
    assert skills.LOADED == ["release", "style"]


def test_a_free_text_search_names_a_matching_skill_beside_the_tools(tree):
    assert "Skill `zoom`" in tool_search.fn("join the meeting")
    assert "tool_search skill:zoom" in tool_search.fn("zoom")
    assert "Skill `" not in tool_search.fn("pdf")


def test_request_load_queues_once_and_take_pending_marks_loaded(tree):
    ok, msg = skills.request_load("Zoom", by="command")
    assert ok and "loads at the next turn" in msg
    assert skills.request_load("zoom")[0] is False
    assert skills.request_load("nope")[0] is False
    got = skills.take_pending()
    assert [(n, by) for n, _t, by in got] == [("zoom", "command")]
    assert got[0][1].startswith("[skill: zoom") and skills.LOADED == ["zoom"] and skills.PENDING == []


def test_the_loaded_set_follows_the_window(tree):
    hist = [{"role": "user", "content": "go"},
            {"role": "tool", "name": "tool_search", "content": skills.render("zoom")},
            {"role": "user", "content": skills.render("release") + "\n\n[Runtime note]"},
            {"role": "assistant", "content": "ok"}]
    assert skills.sync_loaded(hist, 0) == [] and skills.LOADED == ["zoom", "release"]
    assert skills.sync_loaded(hist, 2) == ["zoom"] and skills.LOADED == ["release"]
    assert skills.can_load("zoom")[0]
    assert skills.skills_in(hist[:2]) == ["zoom"]


# ── the picker ────────────────────────────────────────────────────────

def test_parse_pick_keeps_known_names_and_clamps():
    skills.SKILLS["a"] = skills.Skill("a", "/x/a/SKILL.md", "A", "body")
    skills.SKILLS["b"] = skills.Skill("b", "/x/b/SKILL.md", "B", "body")
    c = list(skills.SKILLS.values())
    got = skills.parse_pick('Sure: [{"name": "B", "confidence": 1.7}, {"name": "zz", "confidence": 1}, '
                            '{"name": "a", "confidence": "0.3"}, 5]', c)
    assert got == [("b", 1.0), ("a", 0.3)]
    assert skills.parse_pick("no json", c) is None
    assert skills.parse_pick('{"name": "a"}', c) is None
    assert skills.parse_pick("[]", c) == []


def test_pick_fails_open_on_error_timeout_and_nonsense(tree):
    for judge, err in ((Judge(TimeoutError()), "TimeoutError"), (Judge("nothing here"), "unparseable")):
        v = skills.pick("join the call", source="start", complete=judge)
        assert v["how"] == "failopen" and v["err"] == err and v["picks"] == []
        assert skills.apply_verdict(v) == ([], [])
    assert skills.pick("x", complete=None)["how"] == "failopen"
    assert skills.pick("", complete=Judge())["how"] == "none"
    judge = Judge("[]")
    skills.pick("join the call", complete=judge)
    assert "- zoom: Joining a meeting" in judge.prompts[0] and "secret" not in judge.prompts[0]
    assert "join the call" in judge.prompts[0]


def test_apply_verdict_loads_above_the_threshold_suggests_below_and_caps_loads(tree):
    skills.CFG["max_picks_per_turn"] = 1
    v = {"source": "start", "how": "judge", "ms": 3,
         "picks": [("zoom", 0.9), ("release", 0.8), ("style", 0.5)]}
    # a confident pick past the cap is suggested rather than dropped
    assert skills.apply_verdict(v) == (["zoom"], ["release", "style"])
    assert [n for n, _ in skills.PENDING] == ["zoom"] and skills.SUGGEST == ["release", "style"]
    line = skills.take_suggestions()
    assert line.startswith("A skill may apply: `release` (cutting a release), `style` (the house style")
    assert line.endswith("tool_search skill:release,style.") and skills.take_suggestions() == ""


def test_pick_at_start_uses_the_judge_or_the_keyword_floor(tree):
    skills.CFG["pick"] = "start"
    skills.set_judge(Judge('[{"name": "release", "confidence": 0.95}]'))
    assert skills.pick_at_start("please join a meeting") == (["release"], [])
    skills.reset_session()
    skills.set_judge(Judge(RuntimeError("down")))      # the judge fails: keywords decide
    assert skills.pick_at_start("please join a meeting") == (["zoom"], [])
    skills.reset_session()
    skills.CFG["pick"] = "off"
    assert skills.pick_at_start("Zoom in on the chart; mind the house style") == (["zoom", "style"], [])
    skills.reset_session()
    assert skills.pick_at_start("zoomed foo_zoom") == ([], [])   # whole words only
    assert skills.STATE["started"]


def test_task_text_strips_attachments_and_the_agent_blocks():
    msg = ("@AGENT.md do the thing\n\n[notes.md: 3 lines]\na\nb\nc\n\n[SYSTEM CONTEXT: x]"
           "\n\n[AGENT IDENTITY FILE: AGENT.md]\n`zoom` everywhere")
    assert skills.task_text(msg) == "@AGENT.md do the thing"
    assert len(skills.task_text("w " * 3000)) == skills._PICK_TEXT_CHARS


def test_the_async_picker_keeps_the_newest_verdict_and_drops_an_overtaken_one(tree):
    skills.CFG["pick"] = "always"
    first = Judge('[{"name": "zoom", "confidence": 0.9}]')
    first.block = threading.Event()
    skills.set_judge(first)
    assert skills.PICKER.kick("join a meeting", "user")
    assert skills.PICKER.harvest() is None                 # not ready: nothing applied this turn
    second = Judge('[{"name": "release", "confidence": 0.9}]')
    skills.set_judge(second)
    assert skills.PICKER.kick("cut a release", "user")
    first.block.set()
    for _ in range(50):
        v = skills.PICKER.harvest()
        if v:
            break
        time.sleep(0.05)
    assert v and v["picks"] == [("release", 0.9)]          # the overtaken verdict was dropped
    assert skills.PICKER.harvest() is None
    skills.CFG["pick"] = "off"
    assert skills.PICKER.kick("anything", "user") is False


# ── the loop ──────────────────────────────────────────────────────────

def test_the_request_carries_the_skill_catalog_and_instructions_statically(tree, loop):
    mocks, sent = loop
    mocks.script(_text_turn())
    agent.run_agent_single([{"role": "user", "content": "say hi"}], {"text": "", "up_to": 0}, [], log)
    system = _system(sent[0])
    assert skills.CATALOG_PREFIX in system and skills.INSTRUCTIONS in system
    assert "`zoom`" in system and "secret" not in system
    assert sent[0]["messages"][-1]["role"] == "user"


def test_the_catalog_switch_also_covers_skills(tree, loop, monkeypatch):
    monkeypatch.setitem(agent._TOOLS_CFG, "catalog", False)
    mocks, sent = loop
    mocks.script(_text_turn())
    agent.run_agent_single([{"role": "user", "content": "say hi"}], {"text": "", "up_to": 0}, [], log)
    assert skills.CATALOG_PREFIX not in _system(sent[0])


def test_the_model_loads_a_skill_as_a_tool_result_with_no_tool_list_change(tree, loop):
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "tool_search", '{"query": "skill:zoom"}'), _text_turn())
    history = [{"role": "user", "content": "join the call"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    result = next(m for m in history if m.get("role") == "tool" and m.get("name") == "tool_search")
    assert result["content"].startswith("[skill: zoom (") and "press join" in result["content"]
    names = lambda body: [t["function"]["name"] for t in body.get("tools", [])]
    assert names(sent[1]) == names(sent[0])
    assert skills.LOADED == ["zoom"]


def test_a_keyboard_load_joins_the_next_user_turn_before_the_runtime_note(tree, loop):
    mocks, sent = loop
    skills.request_load("release", by="command")
    mocks.script(_text_turn())
    history = [{"role": "user", "content": "do the release"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    content = history[0]["content"]
    assert content.startswith("do the release\n\n[skill: release (")
    assert content.index("[skill: release") < content.index(agent._RUNTIME_NOTE_HEAD)
    assert len([m for m in history if m["role"] == "user"]) == 1
    assert skills.LOADED == ["release"]


def test_the_start_pick_loads_into_the_first_message_and_suggests_in_the_note(tree, loop):
    skills.CFG["pick"] = "start"
    skills.set_judge(Judge('[{"name": "zoom", "confidence": 0.9}, {"name": "style", "confidence": 0.5}]'))
    mocks, sent = loop
    mocks.script(_text_turn())
    history = [{"role": "user", "content": "join the call"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    content = history[0]["content"]
    assert "[skill: zoom (" in content and "A skill may apply: `style`" in content
    assert skills.LOADED == ["zoom"] and history[0].get("_skill_pick")
    assert sent[0]["messages"][-1]["content"] == content


def test_always_judges_a_later_message_on_a_thread_and_applies_it_at_a_boundary(tree, loop):
    skills.CFG["pick"] = "always"
    skills.STATE["started"] = True
    judge = Judge('[{"name": "release", "confidence": 0.9}]')
    judge.block = threading.Event()        # the judge is slower than this whole run
    skills.set_judge(judge)
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "read_file", '{"path": "x"}'), _text_turn())
    mocks.stub_tool("read_file", "contents")
    history = [{"role": "user", "content": "cut a release"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert history[0].get("_skill_pick") and skills.LOADED == []   # nothing blocked, nothing applied
    judge.block.set()
    for _ in range(100):
        if skills.PICKER._result:
            break
        time.sleep(0.02)
    assert "cut a release" in judge.prompts[0]
    # the next turn boundary applies the verdict: the skill joins a new user turn (the
    # newest message is the model's text) and the window sync keeps it loaded
    assert agent._skills_at_boundary(history, None, 0, log) == ""
    assert skills.LOADED == ["release"] and history[-1]["role"] == "user"
    assert history[-1]["content"].startswith("[skill: release (") and history[-1]["_skills"] == ["release"]
    # ... and one appended to a tool result is still found by the sync
    hist2 = [{"role": "tool", "name": "read_file", "content": "contents"}]
    skills.reset_session()
    skills.request_load("zoom", by="command")
    agent._skills_at_boundary(hist2, None, 0, log)
    assert hist2[0]["content"].startswith("contents\n\n[skill: zoom (") and hist2[0]["_skills"] == ["zoom"]
    assert skills.sync_loaded(hist2, 0) == [] and skills.LOADED == ["zoom"]
    assert agent._compress_repeated_tool_results(
        hist2 * 4, "read_file", log, local_only=True) == []


def test_a_bus_item_is_judged_only_with_pick_on_events(tree, monkeypatch):
    import monitor_bus
    skills.CFG["pick"] = "always"
    kicks = []
    monkeypatch.setattr(skills.PICKER, "kick", lambda text, source: kicks.append((source, text)) or True)
    monitor_bus.inject("the build finished")
    hist = []
    assert agent._drain_monitor_injections(hist) is True
    assert kicks == [] and hist[0].get("_skill_pick")
    skills.CFG["pick_on_events"] = True
    monitor_bus.inject("the build finished again")
    agent._drain_monitor_injections(hist)
    assert kicks == [("events", "the build finished again")]
    monitor_bus.put_user("typed while busy")
    agent._drain_monitor_injections(hist)
    assert kicks[-1] == ("user", "typed while busy")


def test_the_checkpoint_keeps_loaded_skills_and_a_resume_restores_the_names(tree, loop, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(tmp_path / "cp.json"))
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "tool_search", '{"query": "skill:zoom"}'), _text_turn())
    history = [{"role": "user", "content": "join"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    agent._save_checkpoint(history, {"text": "", "up_to": 0}, 2, None)
    assert json.load(open(tmp_path / "cp.json"))["loaded_skills"] == ["zoom"]
    skills.reset_session()
    assert agent._load_checkpoint() is not None
    assert skills.LOADED == ["zoom"] and skills.STATE["started"]
    assert "already loaded" in tool_search.fn("skill:zoom")


def test_the_summarizer_is_told_which_skills_leave_with_the_messages(tree):
    msgs = [{"role": "tool", "name": "tool_search", "content": skills.render("zoom")},
            {"role": "assistant", "content": "ok"}]
    prompt = agent._build_summary_prompt("", msgs)
    assert "these skills (instruction files) were loaded" in prompt and "skill:NAME: zoom" in prompt
    assert "instruction files" not in agent._build_summary_prompt("", msgs[1:])


def test_history_compression_leaves_a_skill_text_whole(tree):
    body = skills.render("zoom") + "x" * 400
    hist = [{"role": "tool", "name": "tool_search", "content": body},
            {"role": "tool", "name": "tool_search", "content": "No tool matches " + "y" * 400},
            {"role": "tool", "name": "tool_search", "content": "No tool matches " + "z" * 400},
            {"role": "tool", "name": "tool_search", "content": "newest"}]
    changed = agent._compress_repeated_tool_results(hist, "tool_search", log, local_only=True)
    assert changed == [2, 1] and hist[0]["content"] == body


# ── the keyboard ──────────────────────────────────────────────────────

def _ctx():
    printed = []
    cb = SimpleNamespace(_print=printed.append, on_notice=lambda *a: printed.append(a))
    return SimpleNamespace(cb=cb, log=log, conversation_history=[], summary_state={"text": "", "up_to": 0},
                           initial_files=None, async_summarizer=None, setup_logger=lambda: (log, "p", None),
                           refresh_cb_log=lambda _l: None), printed


def test_skill_list_load_and_reload_from_the_keyboard(tree):
    ctx, printed = _ctx()
    assert commands.handle_command("/skill", ctx)
    out = printed[-1]
    assert "zoom" in out and "hidden   secret" in out and "picker: off" in out
    commands.handle_command("/skill zoom", ctx)
    assert "loads at the next turn" in printed[-1][-1] and [n for n, _ in skills.PENDING] == ["zoom"]
    commands.handle_command("/skill", ctx)
    assert "queued" in printed[-1]
    commands.handle_command("/skill nope", ctx)
    assert "no skill named nope" in printed[-1][-1]
    (tree / ".agent" / "skills" / "secret" / "SKILL.md").unlink()
    commands.handle_command("/skill reload", ctx)
    assert "secret" not in printed[-1] and "secret" not in skills.SKILLS
    assert "/skill" in " ".join(str(x) for x in printed) or commands.handle_command("/help", ctx)


def test_clear_forgets_the_loaded_skills(tree):
    ctx, _printed = _ctx()
    tool_search.fn("skill:zoom")
    commands.handle_command("/clear", ctx)
    assert skills.LOADED == [] and skills.STATE["started"] is False


def test_the_context_bar_counts_loaded_skills(tree):
    tool_search.fn("skill:zoom")
    bar = agent._render_context_bar([{"role": "user", "content": "x"}], {"text": ""}, 1000)
    assert "skills:" in bar and "1 loaded (zoom)" in bar
    skills.reset_session()
    assert "skills:" not in agent._render_context_bar([], {"text": ""}, 1000)
