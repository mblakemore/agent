"""Tool registry with auto-discovery.

Each tool module in this package should export:
  - fn: the callable implementation
  - definition: the OpenAI-compatible tool schema dict

On import, `_discover_tools()` walks this package and registers every
matching module into `MAP_FN` (name -> callable) and `tools` (schema
list). `load_extra_tools(directory)` can then layer agent-specific
tools from an external directory on top of the shared set.
"""

import importlib
import importlib.util
import logging
import os
import pkgutil
from pathlib import Path

MAP_FN = {}
tools = []

_log = logging.getLogger("agent")

# Max number of agent-specific (extra) tools to load.
# Most recently modified files are loaded first; the rest are skipped.
_MAX_EXTRA_TOOLS = int(os.environ.get("MAX_EXTRA_TOOLS", "10"))


def _discover_tools():
    """Auto-discover and register all tool modules in this package."""
    package_dir = Path(__file__).parent
    for finder, name, ispkg in pkgutil.iter_modules([str(package_dir)]):
        module = importlib.import_module(f".{name}", package=__package__)
        if hasattr(module, "fn") and hasattr(module, "definition"):
            tool_name = module.definition["function"]["name"]
            MAP_FN[tool_name] = module.fn
            tools.append(module.definition)


def _validate_definition(defn, filename):
    """Validate and normalize a tool definition dict.

    Returns the tool name on success, None on failure.
    """
    if not isinstance(defn, dict):
        _log.warning("Skipping %s: definition is not a dict", filename)
        return None
    # Agent-written tools sometimes omit the required "type" wrapper
    if "type" not in defn and "function" in defn:
        defn["type"] = "function"
    if "function" not in defn:
        _log.warning("Skipping %s: definition missing 'function' key", filename)
        return None
    tool_name = defn["function"].get("name")
    if not tool_name:
        _log.warning("Skipping %s: definition missing function name", filename)
        return None
    return tool_name


def load_extra_tools(directory):
    """Load agent-specific tools from an external directory.

    Discovers .py files in the given directory (skipping _-prefixed files),
    sorted by modification time (newest first), loads up to _MAX_EXTRA_TOOLS
    valid tools. Agent tools override shared tools of the same name.

    Args:
        directory: Path to directory containing tool .py files.
    """
    tool_dir = Path(directory)
    if not tool_dir.is_dir():
        return

    # Sort by mtime descending — newest tools load first
    py_files = [f for f in tool_dir.glob("*.py") if not f.name.startswith("_")]
    py_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    loaded = 0
    skipped_cap = []

    for py_file in py_files:
        try:
            spec = importlib.util.spec_from_file_location(
                f"extra_tools.{py_file.stem}", str(py_file))
            if spec is None or spec.loader is None:
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if not (hasattr(module, "fn") and hasattr(module, "definition")):
                continue

            tool_name = _validate_definition(module.definition, py_file.name)
            if not tool_name:
                continue

            # Enforce cap (overrides of existing shared tools don't count)
            is_override = tool_name in MAP_FN
            if not is_override and loaded >= _MAX_EXTRA_TOOLS:
                skipped_cap.append(tool_name)
                continue

            # Register
            defn = module.definition
            if is_override:
                for i, t in enumerate(tools):
                    if t["function"]["name"] == tool_name:
                        tools[i] = defn
                        break
            else:
                tools.append(defn)
                loaded += 1
            MAP_FN[tool_name] = module.fn
            _log.debug("Loaded extra tool: %s from %s", tool_name, py_file.name)
        except Exception as e:
            _log.warning("Failed to load extra tool %s: %s", py_file.name, e)

    if skipped_cap:
        _log.info("Tool cap (%d): skipped %d older tools: %s",
                   _MAX_EXTRA_TOOLS, len(skipped_cap), ", ".join(skipped_cap))


_discover_tools()
