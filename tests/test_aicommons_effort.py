"""Effort controls (aicommons-integration.md P5, §7 E2/M6)."""

import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent
import llm_backend as lb
from tools import subagent as sub

CFG = {"kind": "aicommons", "base_url": "https://gw/v1", "api_key": "k"}


@pytest.fixture(autouse=True)
def _no_fake(monkeypatch):
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)


def test_effort_escalates_one_step_once():
    b = lb.AICommonsBackend({**CFG, "model": "claude-v5.5-opus"})
    assert b.effort == "high"
    assert b.escalate_effort() == "xhigh"
    assert b.escalate_effort() is None
    assert b._thinking_kwargs()["output_config"] == {"effort": "xhigh"}


def test_no_escalation_without_thinking_or_at_the_top():
    assert lb.AICommonsBackend({**CFG, "aicommons": {"thinking": "off"}}).escalate_effort() is None
    assert lb.AICommonsBackend({**CFG, "effort": "max"}).escalate_effort() is None


def test_model_and_effort_flags_rebuild_main(monkeypatch):
    monkeypatch.setitem(agent._config, "backends", {"main": dict(CFG), "summary": {"kind": "llamacpp"}})
    monkeypatch.setattr(agent, "_main_backend", None)
    agent._apply_model_effort_overrides("claude-v5.5-sonnet", "low")
    assert agent._main_backend.model == "claude-v5.5-sonnet" and agent._main_backend.effort == "low"
    assert agent._config["backends"]["main"]["effort"] == "low"     # survives a failover rebuild


def test_subagents_inherit_backend_flags_and_their_own_tier(monkeypatch):
    monkeypatch.delenv(agent._SUBAGENT_ARGS_ENV, raising=False)
    monkeypatch.setitem(agent._config, "subagent", {"model": "claude-v5.5-sonnet", "effort": "low"})
    args = SimpleNamespace(backend_main="aicommons", backend_summary="aicommons",
                           model="claude-v5.5-opus", effort="high")
    out = agent._export_subagent_args(args)
    assert out == ["--backend-main", "aicommons", "--backend-summary", "aicommons",
                   "--model", "claude-v5.5-sonnet", "--effort", "low"]
    assert sub._inherited_args() == out
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stderr="")
        sub.subagent("do a thing")
    cmd = run.call_args[0][0]
    assert cmd[cmd.index("--backend-main") + 1] == "aicommons" and cmd[-1] == "do a thing"


def test_nothing_exported_without_flags(monkeypatch):
    monkeypatch.delenv(agent._SUBAGENT_ARGS_ENV, raising=False)
    monkeypatch.setitem(agent._config, "subagent", {})
    assert agent._export_subagent_args(SimpleNamespace()) == []
    assert agent._SUBAGENT_ARGS_ENV not in os.environ
    assert sub._inherited_args() == []
