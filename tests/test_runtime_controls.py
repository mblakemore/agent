"""Runtime controls a tool may call (tools.RUNTIME), the switch_model tool, /effort, and the
model-switch cache-miss reason (beewatcher/agent-runtime-controls-plan.md, 2026-11-03).

A tool function sees only its arguments. Before this, a tool could not switch the model
or the effort; now agent.py registers those controls and the shipped `switch_model` tool
uses them, gated on `preferences.switchable_models` so the default request is unchanged.
"""

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent
import commands
import llm_backend as lb
import tools as registry
import tools.switch_model as switch_model

log = logging.getLogger("test_runtime_controls")


class _Backend:
    """A backend with the in-place switches the AI Commons one has."""
    def __init__(self, model, effort="high"):
        self.model, self.effort, self.calls = model, effort, []

    def set_model(self, model):
        self.model = model
        self.calls.append(("model", model))

    def set_effort(self, effort):
        self.effort = effort
        self.calls.append(("effort", effort))


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Patched config and backends, cwd in a temp dir so persistence is local."""
    monkeypatch.chdir(tmp_path)
    cfg = {"llm": {"kind": "aicommons", "model": "model-a"}, "summary": {"model": "model-s"},
           "backends": {"main": {"kind": "aicommons", "model": "model-a"}, "summary": {"model": "model-s"}},
           "preferences": {"switchable_models": ["model-a", "model-b"]}}
    main, summ = _Backend("model-a"), _Backend("model-s", "low")
    monkeypatch.setattr(agent, "_config", cfg)
    monkeypatch.setattr(agent, "_main_backend", main)
    monkeypatch.setattr(agent, "_summary_backend", summ)
    return SimpleNamespace(cfg=cfg, main=main, summary=summ, persisted=tmp_path / ".agent" / "config.json")


def _persisted(h):
    return json.loads(h.persisted.read_text())


# ── the registry ─────────────────────────────────────────────────────────────

class TestRegistry:
    def test_agent_registers_the_five_controls(self):
        assert set(registry.RUNTIME) >= {"get_model", "set_model", "models", "get_effort", "set_effort"}
        # Behaviour, not identity: coverage tests load agent.py a second time, and the
        # copy that registered last owns the gate; both read the config live.
        assert callable(registry.GATES.get("switch_model"))

    def test_set_model_flips_config_backend_and_file(self, harness):
        registry.RUNTIME["set_model"]("main", "model-b")
        assert harness.cfg["llm"]["model"] == "model-b" and harness.main.model == "model-b"
        assert harness.cfg["backends"]["main"]["model"] == "model-b"
        assert _persisted(harness)["llm"]["model"] == "model-b"
        assert registry.RUNTIME["get_model"]("main") == "model-b"
        assert registry.RUNTIME["get_model"]("summary") == "model-s"

    def test_unknown_role_and_empty_model_raise(self, harness):
        with pytest.raises(ValueError):
            registry.RUNTIME["set_model"]("advisor", "model-b")
        with pytest.raises(ValueError):
            registry.RUNTIME["set_model"]("main", "  ")
        with pytest.raises(ValueError):
            registry.RUNTIME["models"]("nope")

    def test_models_reads_a_list_or_a_per_role_map(self, harness):
        assert registry.RUNTIME["models"]("main") == ["model-a", "model-b"]
        assert registry.RUNTIME["models"]("summary") == []
        harness.cfg["preferences"]["switchable_models"] = {"main": ["x"], "summary": [" y ", ""]}
        assert registry.RUNTIME["models"]("main") == ["x"]
        assert registry.RUNTIME["models"]("summary") == ["y"]
        harness.cfg["preferences"]["switchable_models"] = "model-a"
        assert registry.RUNTIME["models"]("main") == []

    def test_set_effort_applies_and_persists(self, harness):
        registry.RUNTIME["set_effort"]("main", "Medium")
        assert harness.main.effort == "medium" and harness.cfg["llm"]["effort"] == "medium"
        assert harness.cfg["backends"]["main"]["effort"] == "medium"
        assert _persisted(harness)["llm"]["effort"] == "medium"
        assert registry.RUNTIME["get_effort"]("main") == "medium"
        assert registry.RUNTIME["get_effort"]("summary") == "low"
        with pytest.raises(ValueError):
            registry.RUNTIME["set_effort"]("main", "extreme")

    def test_a_backend_without_set_effort_gets_the_attribute(self, harness, monkeypatch):
        monkeypatch.setattr(agent, "_main_backend", SimpleNamespace(model="m", effort="high"))
        agent._set_effort_for_role("main", "low")
        assert agent._main_backend.effort == "low"
        monkeypatch.setattr(agent, "_main_backend", SimpleNamespace(model="m"))
        agent._set_effort_for_role("main", "xhigh")       # a backend with no effort: config only
        assert agent._get_effort_for_role("main") == "xhigh"

    def test_a_model_switch_keeps_the_effort(self, harness):
        harness.main.effort = "medium"
        agent._set_model_for_role("main", "model-b")
        assert harness.main.effort == "medium"


# ── the switch_model tool ────────────────────────────────────────────────────

class TestSwitchModelTool:
    def test_hidden_and_gated_off_by_default(self, harness):
        harness.cfg["preferences"] = {}
        assert not registry.available("switch_model")
        assert "switch_model" not in registry.catalog_line()
        assert not registry.request_load("switch_model")
        assert not switch_model.definition["function"]["parameters"]["required"]

    def test_the_tier_opens_with_the_list(self, harness):
        saved = dict(registry.TIERS)
        try:
            registry.TIERS["switch_model"] = "hidden"
            registry.rebuild()
            assert registry.available("switch_model")
            agent._apply_switch_model_tier()
            assert registry.TIERS["switch_model"] == "deferred"
            assert "`switch_model` (switch the model at runtime)" in registry.catalog_line()
            harness.cfg["preferences"] = {}
            assert not registry.available("switch_model"), "the gate reads the config live"
        finally:
            registry.TIERS.clear()
            registry.TIERS.update(saved)
            registry.rebuild()

    def test_default_registry_state_is_the_shipped_one(self):
        # With the test config (no switchable_models) nothing the model sees changed.
        assert registry.TIERS.get("switch_model") == "hidden"
        assert all(d["function"]["name"] != "switch_model" for d in registry.tools)

    def test_reports_with_no_model(self, harness):
        out = switch_model.fn()
        assert out == "main model: model-a; switchable: model-a, model-b"
        assert switch_model.fn(role="summary") == (
            "summary model: model-s; switchable: none (preferences.switchable_models is empty)")

    def test_switches_and_persists(self, harness):
        out = switch_model.fn("model-b")
        assert out.startswith("main model switched to model-b (was model-a)")
        assert "cold prompt cache" in out
        assert harness.main.model == "model-b" and _persisted(harness)["llm"]["model"] == "model-b"

    def test_refuses_off_the_list_and_no_ops_on_the_same(self, harness):
        out = switch_model.fn("model-z")
        assert out.startswith("Error: 'model-z' is not a switchable main model (model-a, model-b)")
        assert "preferences.switchable_models" in out
        assert harness.main.calls == []
        assert switch_model.fn("model-a") == "main model is already model-a; nothing changed"
        assert switch_model.fn("model-b", role="advisor").startswith("Error: unknown role")

    def test_per_role_lists(self, harness):
        harness.cfg["preferences"]["switchable_models"] = {"summary": ["model-s", "model-t"]}
        assert switch_model.fn("model-t", role="summary").startswith("summary model switched to model-t")
        assert harness.summary.model == "model-t"
        assert switch_model.fn("model-b").startswith("Error: 'model-b' is not a switchable main model (none)")

    def test_a_failing_switch_is_reported(self, harness, monkeypatch):
        def boom(role, model):
            raise RuntimeError("no backend")
        monkeypatch.setitem(registry.RUNTIME, "set_model", boom)
        assert switch_model.fn("model-b") == "Error: switch to model-b failed: no backend"

    def test_without_a_harness_registry(self, monkeypatch):
        monkeypatch.setattr(registry, "RUNTIME", {})
        assert switch_model.fn("model-b").startswith("Error: this harness exposes no runtime model control")


# ── /effort ──────────────────────────────────────────────────────────────────

def _ctx(**over):
    calls = []
    base = SimpleNamespace(
        cb=MagicMock(), log=MagicMock(),
        get_effort=lambda role: {"main": "high", "summary": "low"}[role],
        set_effort=lambda role, level: calls.append((role, level)) or (
            (_ for _ in ()).throw(ValueError("bad")) if level == "max" else None),
        calls=calls)
    for k, v in over.items():
        setattr(base, k, v)
    return base


def _notices(ctx):
    return [(c.args[0], c.args[1]) for c in ctx.cb.on_notice.call_args_list]


class TestEffortCommand:
    def test_bare_shows_both_roles(self):
        ctx = _ctx()
        assert commands.handle_command("/effort", ctx)
        assert _notices(ctx) == [("info", "effort — main: high, summary: low")]
        assert ctx.calls == []

    def test_sets_main_by_default_and_a_named_role(self):
        ctx = _ctx()
        assert commands.handle_command("/effort medium", ctx)
        assert commands.handle_command("/effort summary low", ctx)
        assert ctx.calls == [("main", "medium"), ("summary", "low")]
        assert ("success", "main effort set to medium (persisted to ./.agent/config.json)") in _notices(ctx)

    def test_bad_level_or_extra_words_warn(self):
        ctx = _ctx()
        commands.handle_command("/effort extreme", ctx)
        commands.handle_command("/effort main low now", ctx)
        assert [k for k, _ in _notices(ctx)] == ["warn", "warn"] and ctx.calls == []

    def test_a_backend_refusal_is_a_warning(self):
        ctx = _ctx()
        commands.handle_command("/effort max", ctx)
        assert _notices(ctx) == [("warn", "bad")]

    def test_help_lists_it(self):
        ctx = _ctx(cb=MagicMock())
        commands.handle_command("/help", ctx)
        assert "/effort" in ctx.cb._print.call_args[0][0]


# ── the backend's set_effort ─────────────────────────────────────────────────

def test_backend_set_effort(monkeypatch):
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    b = lb.AICommonsBackend({"kind": "aicommons", "base_url": "https://gw/v1", "api_key": "k",
                             "model": "claude-v5.5-opus"})
    assert b.effort == "high"
    b.set_effort("Low")
    assert b.effort == "low" and b._thinking_kwargs()["output_config"] == {"effort": "low"}
    with pytest.raises(ValueError):
        b.set_effort("extreme")


# ── the cache-miss reason ────────────────────────────────────────────────────

def _turn_with_usage(tid, read, uncached):
    call = {"index": 0, "id": tid, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": read + uncached, "completion_tokens": 5,
                       "cache_read_input_tokens": read, "cache_creation_input_tokens": 0,
                       "input_tokens": uncached}}]


def _lines(agent_loop_mocks, monkeypatch, prev_model, model, read, uncached):
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model=model, honours_system=True, base_url="http://x"))
    monkeypatch.setattr(agent, "_prefix_unchanged", lambda a, b: True)
    monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - 10,
                                              "prev_model": prev_model})
    agent_loop_mocks.stub_tool("read_file", "contents")
    agent_loop_mocks.script(_turn_with_usage("t1", read, uncached))
    lg = MagicMock()
    agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], lg)
    return [c.args[0] % c.args[1:] for c in lg.info.call_args_list
            if c.args and str(c.args[0]).startswith(("cache.miss", "cache.lifetime"))]


def test_a_miss_right_after_a_model_switch_is_named_and_teaches_nothing(agent_loop_mocks, monkeypatch):
    lines = _lines(agent_loop_mocks, monkeypatch, "model-a", "model-b", 0, 950)
    assert any("reason=model-switch" in l for l in lines), lines
    assert not any(l.startswith("cache.lifetime") for l in lines), lines
    assert agent._CACHE_REQ["prev_model"] == "model-b"


def test_the_same_model_is_still_the_provider(agent_loop_mocks, monkeypatch):
    lines = _lines(agent_loop_mocks, monkeypatch, "model-b", "model-b", 0, 950)
    assert any("reason=provider" in l for l in lines), lines
