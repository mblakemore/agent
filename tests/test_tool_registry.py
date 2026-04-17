import os
import shutil
import tempfile
import pytest
from tools import MAP_FN, tools, load_extra_tools

@pytest.fixture
def temp_tool_dir():
    dir_path = tempfile.mkdtemp()
    yield dir_path
    shutil.rmtree(dir_path)

def test_load_extra_tools_valid(temp_tool_dir):
    # Create a valid tool file
    tool_content = """
def fn(x):
    return x
definition = {
    "type": "function",
    "function": {
        "name": "test_tool",
        "description": "A test tool",
        "parameters": {"type": "object", "properties": {}}
    }
}
"""
    with open(os.path.join(temp_tool_dir, "test_tool.py"), "w") as f:
        f.write(tool_content)
    
    initial_map_size = len(MAP_FN)
    load_extra_tools(temp_tool_dir)
    assert "test_tool" in MAP_FN
    assert len(MAP_FN) > initial_map_size

def test_load_extra_tools_invalid_definition(temp_tool_dir):
    # Create a tool file missing 'definition'
    tool_content = "def fn(x): return x"
    with open(os.path.join(temp_tool_dir, "invalid_tool.py"), "w") as f:
        f.write(tool_content)
    
    initial_map_size = len(MAP_FN)
    load_extra_tools(temp_tool_dir)
    assert len(MAP_FN) == initial_map_size

def test_load_extra_tools_missing_function_name(temp_tool_dir):
    # Create a tool file with a definition missing 'name'
    tool_content = """
def fn(x): return x
definition = {
    "type": "function",
    "function": {
        "description": "Missing name"
    }
}
"""
    with open(os.path.join(temp_tool_dir, "missing_name.py"), "w") as f:
        f.write(tool_content)
    
    initial_map_size = len(MAP_FN)
    load_extra_tools(temp_tool_dir)
    assert len(MAP_FN) == initial_map_size

def test_load_extra_tools_cap(temp_tool_dir):
    # Create more tools than the cap (default 10)
    # We can set MAX_EXTRA_TOOLS env var for this test
    os.environ["MAX_EXTRA_TOOLS"] = "2"
    import tools
    # Re-read the cap from the module
    # Note: _MAX_EXTRA_TOOLS is set at import time. 
    # We might need to reload or manually set it if the module is already imported.
    # For this test, we will just create 5 tools and see if only some are loaded.
    
    # Since tools is already imported, we might need to manually set the internal var
    # if we want to test the cap strictly.
    import tools
    tools._MAX_EXTRA_TOOLS = 2
    
    for i in range(5):
        content = f"""
def fn(x): return x
definition = {{
    "type": "function",
    "function": {{ "name": "tool_{i}" }}
}}
"""
        with open(os.path.join(temp_tool_dir, f"tool_{i}.py"), "w") as f:
            f.write(content)
            
    initial_map_size = len(MAP_FN)
    load_extra_tools(temp_tool_dir)
    
    # Only 2 new tools should be added (plus any overrides, but these are all new)
    # However, the current implementation of load_extra_tools uses the global MAP_FN.
    # We need to check how many 'tool_x' were added.
    added_tools = [k for k in MAP_FN.keys() if k.startswith("tool_")]
    assert len(added_tools) <= 2 + (initial_map_size - len([k for k in MAP_FN.keys() if not k.startswith("tool_")]))
    # Better: check specifically for the 5 we added.
    # Since we don't know existing tools, we check the delta.
    # This is a bit messy due to global state.
