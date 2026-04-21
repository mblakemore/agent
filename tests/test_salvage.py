import pytest
import logging
from agent import _salvage_tool_args

def test_salvage_json_cleanup():
    log = logging.getLogger("test")
    # Test a case that should be salvaged
    raw = "file,path:test.txt,action:read"
    res = _salvage_tool_args("file", raw, log)
    assert res is not None
    assert res["action"] == "read"
    assert res["path"] == "test.txt"
