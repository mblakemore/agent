"""The TUI footer reads config['llm']['model']; the startup banner uses
backend.model. _sync_config_model keeps them in agreement so the footer shows
the active (e.g. auto-detected gateway) model, not the llamacpp default.
"""

import agent


class _FakeBackend:
    def __init__(self, model):
        self.model = model


def test_sync_updates_config_to_backend_model():
    cfg = {"llm": {"model": "gemma-4-31B"}}
    agent._sync_config_model(_FakeBackend("claude-v4.6-sonnet"), cfg)
    assert cfg["llm"]["model"] == "claude-v4.6-sonnet"


def test_sync_noop_when_backend_model_empty():
    cfg = {"llm": {"model": "gemma-4-31B"}}
    agent._sync_config_model(_FakeBackend(""), cfg)
    agent._sync_config_model(_FakeBackend(None), cfg)
    assert cfg["llm"]["model"] == "gemma-4-31B"


def test_sync_creates_llm_section_if_missing():
    cfg = {}
    agent._sync_config_model(_FakeBackend("m1"), cfg)
    assert cfg["llm"]["model"] == "m1"


def test_sync_handles_backend_without_model_attr():
    cfg = {"llm": {"model": "keep"}}
    agent._sync_config_model(object(), cfg)  # no .model attribute
    assert cfg["llm"]["model"] == "keep"
