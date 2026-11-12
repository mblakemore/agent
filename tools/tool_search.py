"""tool_search — find and load the tools that are not in the model's list.

The model's list carries the core tier only; the deferred tools are named in one line
of the system prompt and the hidden ones not at all. This tool searches the registry
(`tools.search`) and asks for the matches to be loaded (`tools.request_load`); the
agent loop appends them to the session's tool list once per turn, so they are usable
from the next request. Loads are sticky for the session.
"""

import tools as _registry

_tier = "core"
_short = "find and load more tools"
_aliases = ["ToolSearch", "search_tools", "find_tool", "load_tool"]
_keywords = ["tools", "capability", "load", "schema"]


def fn(query: str = "", max_results: int = 5) -> str:
    """Find tools that are not in your list and load them for this session.

    Args:
        query: keywords (e.g. "pdf", "screenshot page"), or "select:name,name" to load by name.
        max_results: how many matches to load at most (default 5).
    """
    q = str(query or "").strip()
    if not q:
        return ("Error: give keywords (for example 'pdf' or 'screenshot page'), or "
                "select:name,name to load by name, or skill:NAME for a skill. "
                + _registry.catalog_text())
    try:
        limit = max(1, int(max_results))
    except (TypeError, ValueError):
        limit = 5
    if q.lower().startswith("skill:"):
        # A skill's text IS the answer: appended as this tool result, it costs no cache
        # miss and is in the window from now (skills.py).
        import skills as _skills
        wanted = [n.strip() for n in q[len("skill:"):].split(",") if n.strip()]
        if not wanted:
            return "Error: skill:NAME names the skill to load. " + _skills.catalog_text()
        return _skills.load_text(wanted, by="model")
    unknown = []
    if q.lower().startswith("select:"):
        wanted = [n.strip() for n in q[len("select:"):].split(",") if n.strip()]
        known = []
        for n in wanted:
            r = _registry.resolve(n)
            if r and _registry.available(r):
                if r not in known:
                    known.append(r)
            else:
                unknown.append(n)
    else:
        known = _registry.search(q, limit)
    skill_lines = _skill_matches(q) if not q.lower().startswith("select:") else []
    if not known:
        tail = (" Unknown: " + ", ".join(unknown) + ".") if unknown else ""
        return (f'No tool matches "{q}".{tail} ' + _registry.catalog_text(include_hidden=True)
                + ("\n" + "\n".join(skill_lines) if skill_lines else ""))
    lines = []
    loaded = []
    for n in known[:limit]:
        if _registry.is_core(n) or _registry.is_loaded(n):
            lines.append(f"{_registry.describe(n)} (already available)")
        else:
            _registry.request_load(n)
            loaded.append(n)
            lines.append(_registry.describe(n))
    if unknown:
        lines.append("Unknown: " + ", ".join(unknown))
    if loaded:
        lines.append(f"Loaded {len(loaded)} tool(s); they are in your tool list from the next turn.")
    else:
        lines.append("Nothing new to load; these are already in your list.")
    lines += skill_lines
    return "\n".join(lines)


def _skill_matches(query):
    """Skills a free-text query names, listed beside the tool matches (not loaded)."""
    try:
        import skills as _skills
        hits = _skills.search(query)
    except Exception:
        return []
    return [f"Skill `{n}` ({_skills.SKILLS[n].short()}): load it with tool_search skill:{n}"
            for n in hits[:3]]


definition = {
    "type": "function",
    "function": {
        "name": "tool_search",
        "description": (
            "Find and load tools that are not in your list. Query by keywords ('pdf', "
            "'screenshot page', 'schedule a prompt'), or 'select:name,name' to load by name. "
            "Loaded tools stay for the session and are usable from the next turn. Loading "
            "changes your tool list, which costs one cache miss, so load what the task needs "
            "in one call. The short list of other tools is in your instructions. "
            "'skill:NAME' (or skill:NAME,NAME) answers with a skill's instructions instead; "
            "the skills are listed in your instructions when there are any."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords, select:name,name, or skill:NAME.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "How many matches to load at most (default 5).",
                },
            },
            "required": ["query"],
        },
    },
}
