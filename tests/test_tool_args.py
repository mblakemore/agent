"""tool_args.py: the argument repair left agent.py; the old names are aliases."""
import logging

import agent
import tool_args


def test_aliases_are_the_module_objects():
    assert agent._sanitize_tool_args is tool_args.sanitize_tool_args
    assert agent._repair_tool_args is tool_args.repair_tool_args
    assert agent._salvage_tool_args is tool_args.salvage_tool_args
    assert agent._detect_harmony_token is tool_args.detect_harmony_token
    assert agent._FILE_ACTIONS is tool_args.FILE_ACTIONS


def test_repair_goes_through_sanitize(monkeypatch):
    seen = []
    monkeypatch.setattr(tool_args, "_json_repair", None)
    monkeypatch.setattr(tool_args, "salvage_tool_args", lambda n, raw, log: seen.append(raw) or {"path": "x"})
    assert tool_args.repair_tool_args("file", "{broken", logging.getLogger("t")) == {"path": "x"}
    assert seen == ["{broken"]


def test_harmony_token_is_found_in_nested_args():
    hit = tool_args.detect_harmony_token({"path": "ok", "items": ["a", "b<|tool_call|>"]})
    assert hit == ("items.[1]", "<|tool_call|>")
    assert tool_args.detect_harmony_token({"path": "clean"}) is None
