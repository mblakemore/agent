"""agent_config reads the agent's state through DEPS at call time, owns the one piece of
state it has (the seed warning), and edits the backends registry the way the CLI flags ask."""
import logging

import pytest

import agent
import agent_config


def test_configure_rejects_unknown_deps():
    with pytest.raises(TypeError):
        agent_config.configure(nope=lambda: 1)


def test_deps_are_read_at_call_time(monkeypatch):
    seen = {"backends": {"main": {"kind": "llamacpp", "model": "m"}},
            "aicommons": {"base_url": "https://g", "thinking": "off"}}
    monkeypatch.setitem(agent_config.DEPS, "config", lambda: seen)
    entry = agent_config.with_role({"main": {"kind": "aicommons", "aicommons": {"thinking": "on"}}}, "main")
    assert entry["role"] == "main"
    assert entry["aicommons"] == {"base_url": "https://g", "thinking": "on"}   # the role's keys win


def test_agent_registers_its_own_state():
    assert agent_config.DEPS["config"]() is agent._config
    assert agent_config.DEPS["main_backend"]() is agent._main_backend
    assert agent_config.DEPS["summary_backend"]() is agent._summary_backend


def test_seed_warning_has_one_owner():
    assert not hasattr(agent, "_SEED_WARNED")
    assert agent._DEFAULT_CONFIG is agent_config.DEFAULTS
    assert agent._load_config is agent_config.load


def test_override_backend_kind(monkeypatch, caplog):
    cfg = {"backends": {"main": {"kind": "llamacpp", "model": "local-7b", "base_url": "http://127.0.0.1:8080",
                                 "api_key": "k"},
                        "summary": {"kind": "bedrock", "model": "claude-v4.5-haiku"}}}
    monkeypatch.setitem(agent_config.DEPS, "config", lambda: cfg)
    agent_config.override_backend_kind("main", None)
    assert cfg["backends"]["main"]["kind"] == "llamacpp"                      # no flag, no change
    agent_config.override_backend_kind("main", "aicommons")
    main = cfg["backends"]["main"]
    assert main["kind"] == "aicommons" and main["model"] == agent_config.AICOMMONS_DEFAULT_MODELS["main"]
    assert "base_url" not in main and "api_key" not in main                  # the old kind's endpoint goes
    with caplog.at_level(logging.INFO, logger="agent"):
        agent_config.override_backend_kind("summary", "bedrock")
    assert cfg["backends"]["summary"]["model"] == "claude-v4.5-haiku"         # same kind keeps its model
    assert not [r for r in caplog.records if "backend-override" in r.getMessage()]
    agent_config.override_backend_kind("main", "bedrock")
    assert cfg["backends"]["main"]["model"] == agent_config.BEDROCK_DEFAULT_MODELS["main"]
