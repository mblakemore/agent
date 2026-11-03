"""Tool registry with auto-discovery and lazy loading.

Each tool module in this package should export:
  - fn: the callable implementation
  - definition: the OpenAI-compatible tool schema dict

and may export:
  - _tier: "core" (schema in every request), "deferred" (named in one catalog line,
    loaded on request) or "hidden" (found only by tool_search). Built-ins without one
    are core when named in DEFAULT_CORE, else deferred. _auto_exclude = True means
    hidden and, with a gate in GATES, unavailable until the gate opens.
  - _short: a phrase for the catalog line (else the first clause of the description)
  - _aliases: names other harnesses use for the same tool ("bash" for exec_command)
  - _keywords: extra words the search should match

On import, `_discover_tools()` walks this package and registers every matching module
into `MAP_FN` (name -> callable, every tool), `DEFINITIONS` (name -> schema, every
tool, in registration order) and `tools` (the schema list the model sees in every
request: the core tier). `load_extra_tools(directory)` layers agent-specific tools
from an external directory on top. `apply_tiers()` applies the agent's config.

Lazy loading: `tool_search` (a core tool) and the dispatcher in agent.py ask for a
tool with `request_load(name)`; the agent loop drains PENDING_LOADS into the session's
tool list once per turn (`agent._apply_tool_loads`) and records it in LOADED, which the
checkpoint saves so a resumed session has the same list.
"""

import importlib
import importlib.util
import logging
import os
import re
from pathlib import Path

MAP_FN = {}
tools = []            # the definitions the model sees in every request (the core tier)
DEFINITIONS = {}      # every registered tool, name -> definition, in registration order
META = {}             # name -> {"short", "aliases", "keywords"}
TIERS = {}            # name -> "core" | "deferred" | "hidden"
GATES = {}            # name -> callable() -> bool; closed = not available (not searchable, not loadable)
RUNTIME = {}          # name -> callable: what a tool may do to the running harness (agent.py fills it:
                      # get_model / set_model / models / get_effort / set_effort). Empty under another harness.
PENDING_LOADS = []    # names asked for this turn; agent.py drains them once per turn
LOADED = []           # names loaded this session, in load order (saved in the checkpoint)
RESUME_LOADED = []    # names a checkpoint restored; moved into PENDING_LOADS at session start

_log = logging.getLogger("agent")

TIER_VALUES = ("core", "deferred", "hidden")

# The tools that handle most requests on their own: a shell, the file operations, a
# search, a wait, the task list, and the search for everything else.
DEFAULT_CORE = frozenset({
    "exec_command", "read_file", "write_file", "edit_file", "list_files",
    "search_files", "sleep", "task_tracker", "tool_search",
})

# Max number of agent-specific (extra) tools to load; 0 = no cap (the default since
# deferred tools cost a few tokens each). Most recently modified files load first.
_MAX_EXTRA_TOOLS = int(os.environ.get("MAX_EXTRA_TOOLS", "0") or 0)

_WORD = re.compile(r"[a-z0-9]+")   # underscores split: read_pdf is "read" and "pdf"
_STOP = frozenset({"a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "with",
                   "this", "that", "it", "is", "be", "as", "at", "by", "from", "use", "tool"})


def _first_clause(text, limit=60):
    text = " ".join(str(text or "").split())
    cut = re.split(r"[.:;(\n]| - | — ", text, maxsplit=1)[0].strip()
    if len(cut) > limit:
        cut = cut[:limit].rsplit(" ", 1)[0].rstrip(",") + "…"
    return cut[:1].lower() + cut[1:] if cut else ""


def _words(text):
    return {w for w in _WORD.findall(str(text or "").lower()) if len(w) > 1 and w not in _STOP}


def _register(name, fn, definition, module=None, default_tier=None, keep_tier=False):
    """Register one tool. keep_tier: an override of an existing tool keeps its tier."""
    MAP_FN[name] = fn
    DEFINITIONS[name] = definition
    func = definition.get("function") or {}
    desc = func.get("description") or ""
    tier = getattr(module, "_tier", None) if module is not None else None
    if module is not None and getattr(module, "_auto_exclude", False):
        tier = "hidden"
    if tier not in TIER_VALUES:
        tier = default_tier or ("core" if name in DEFAULT_CORE else "deferred")
    if not (keep_tier and name in TIERS):
        TIERS[name] = tier
    aliases = getattr(module, "_aliases", None) if module is not None else None
    keywords = getattr(module, "_keywords", None) if module is not None else None
    short = getattr(module, "_short", None) if module is not None else None
    META[name] = {
        "short": str(short).strip() if short else _first_clause(desc),
        "aliases": [str(a).lower() for a in (aliases or []) if str(a).strip()],
        "keywords": [str(k).lower() for k in (keywords or []) if str(k).strip()],
    }
    rebuild()


def rebuild():
    """The schema list the model sees: the core tier, in registration order."""
    tools[:] = [DEFINITIONS[n] for n in DEFINITIONS if TIERS.get(n) == "core"]


def _discover_tools():
    """Auto-discover and register all tool modules in this package."""
    import pkgutil
    package_dir = Path(__file__).parent
    for finder, name, ispkg in pkgutil.iter_modules([str(package_dir)]):
        module = importlib.import_module(f".{name}", package=__package__)
        if hasattr(module, "fn") and hasattr(module, "definition"):
            _register(module.definition["function"]["name"], module.fn, module.definition, module)


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

    Discovers .py files in the given directory (skipping _-prefixed files), sorted by
    modification time (newest first). Extra tools are deferred unless the module says
    otherwise; an extra that overrides a shared tool of the same name keeps that tool's
    tier. MAX_EXTRA_TOOLS caps how many new tools load (0, the default, is no cap).

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

            # Enforce the cap, when there is one (overrides of existing tools don't count)
            is_override = tool_name in MAP_FN
            if not is_override and _MAX_EXTRA_TOOLS and loaded >= _MAX_EXTRA_TOOLS:
                skipped_cap.append(tool_name)
                continue

            _register(tool_name, module.fn, module.definition, module,
                      default_tier="deferred", keep_tier=is_override)
            if not is_override:
                loaded += 1
            _log.debug("Loaded extra tool: %s from %s", tool_name, py_file.name)
        except Exception as e:
            _log.warning("Failed to load extra tool %s: %s", py_file.name, e)

    if skipped_cap:
        _log.info("Tool cap (%d): skipped %d older tools: %s",
                  _MAX_EXTRA_TOOLS, len(skipped_cap), ", ".join(skipped_cap))


def remove_tool(name: str) -> bool:
    """Remove a tool from the registry by name.

    Returns True if the tool was found and removed, False if it was not
    registered.  Safe to call even if the tool was never loaded.
    """
    was_present = name in MAP_FN
    MAP_FN.pop(name, None)
    DEFINITIONS.pop(name, None)
    TIERS.pop(name, None)
    META.pop(name, None)
    rebuild()
    return was_present


# ── Tiers ─────────────────────────────────────────────────────────────

def set_tier(name, tier):
    if name not in DEFINITIONS or tier not in TIER_VALUES:
        return False
    TIERS[name] = tier
    rebuild()
    return True


def apply_tiers(config_tools=None, whitelist=None, log=None):
    """Apply the agent's config: `preferences.tools_whitelist` first (a tool not on it
    does not exist to the model or the search; MAP_FN keeps it for harness-internal
    calls), then the `tools` block's core/deferred/hidden lists. A whitelist with no
    tier lists keeps the old meaning: everything on it is core."""
    log = log or _log
    config_tools = config_tools if isinstance(config_tools, dict) else {}
    lists = {t: [str(n) for n in (config_tools.get(t) or []) if isinstance(n, str)]
             for t in TIER_VALUES}
    if whitelist and isinstance(whitelist, list):
        keep = set(whitelist)
        for name in [n for n in DEFINITIONS if n not in keep]:
            DEFINITIONS.pop(name, None)
            TIERS.pop(name, None)
            META.pop(name, None)
        if not any(lists.values()):
            for name in DEFINITIONS:
                if TIERS.get(name) != "hidden":   # gated tools stay gated
                    TIERS[name] = "core"
    seen = {}
    for tier in TIER_VALUES:
        for name in lists[tier]:
            if name not in DEFINITIONS:
                log.warning("tools.%s names an unknown tool: %s (ignored)", tier, name)
                continue
            if name in seen:
                log.warning("tools.%s: %s is already in tools.%s (ignored)", tier, name, seen[name])
                continue
            seen[name] = tier
            TIERS[name] = tier
    rebuild()
    return tier_counts()


def tier_counts():
    counts = {t: 0 for t in TIER_VALUES}
    for name in DEFINITIONS:
        counts[TIERS.get(name, "deferred")] += 1
    return counts


def available(name):
    """Registered and not behind a closed gate."""
    if name not in DEFINITIONS:
        return False
    gate = GATES.get(name)
    try:
        return bool(gate()) if gate else True
    except Exception:
        return False


def is_core(name):
    return TIERS.get(name) == "core"


def is_loaded(name):
    return name in LOADED


def names(tier=None):
    return [n for n in DEFINITIONS if tier is None or TIERS.get(n) == tier]


# ── The catalog line ──────────────────────────────────────────────────

CATALOG_PREFIX = "More tools, loaded on request with tool_search:"


def catalog_line():
    """One static line naming the deferred tools, in registration order (so the cached
    prefix is the same bytes every request). Empty when there are none."""
    items = [f"`{n}` ({META[n]['short']})" if META[n]["short"] else f"`{n}`"
             for n in DEFINITIONS if TIERS.get(n) == "deferred" and available(n)]
    if not items:
        return ""
    return f"{CATALOG_PREFIX} {', '.join(items)}."


def catalog_text(include_hidden=False):
    """For a tool_search answer with no match: what there is to load."""
    deferred = [n for n in DEFINITIONS if TIERS.get(n) == "deferred" and available(n)]
    hidden = [n for n in DEFINITIONS if TIERS.get(n) == "hidden" and available(n)]
    parts = []
    if deferred:
        parts.append("Available to load: " + ", ".join(deferred))
    if include_hidden and hidden:
        parts.append("Also: " + ", ".join(hidden))
    return ". ".join(parts) + ("." if parts else "")


# ── Search, aliases, loading ──────────────────────────────────────────

def resolve(name):
    """The tool a name means here: itself, or the tool whose alias it is; None if neither."""
    if not isinstance(name, str) or not name:
        return None
    if name in DEFINITIONS:
        return name
    low = name.lower()
    if low in DEFINITIONS:
        return low
    for n in DEFINITIONS:
        if low in META[n]["aliases"]:
            return n
    return None


def _score(name, words):
    meta = META[name]
    func = DEFINITIONS[name].get("function") or {}
    strong = _words(name) | _words(name.replace("_", " ")) | set(meta["aliases"]) | set(meta["keywords"])
    for a in meta["aliases"]:
        strong |= _words(a)
    weak = _words(func.get("description"))
    for pname, prop in ((func.get("parameters") or {}).get("properties") or {}).items():
        weak |= _words(pname) | _words((prop or {}).get("description") if isinstance(prop, dict) else "")
    name_words = _words(name)
    score = 0
    for w in words:
        if w in name_words:      # the tool's own name beats an alias ("pdf" is read_pdf before "run" is exec_command)
            score += 5
        elif w in strong:
            score += 3
        elif w in weak:
            score += 1
    return score


def search(query, max_results=5):
    """Tools matching a keyword query, best first: distinct query words matched, name,
    alias and keyword matches counting three times a description match; ties keep
    registration order. Only available tools; any tier."""
    words = _words(query)
    if not words:
        return []
    exact = resolve(query.strip())
    scored = []
    for i, name in enumerate(DEFINITIONS):
        if not available(name):
            continue
        s = _score(name, words)
        if exact == name:
            s += 100
        if s > 0:
            scored.append((-s, i, name))
    scored.sort()
    return [n for _, _, n in scored[:max(1, int(max_results or 5))]]


def suggest(name, args=None):
    """For an unknown tool name: the closest tool, as `name (short)`, or ''."""
    text = str(name or "")
    if isinstance(args, dict):
        text += " " + " ".join(str(k) for k in args.keys())
    hits = search(text, 1)
    if not hits:
        return ""
    n = hits[0]
    return f"{n} ({META[n]['short']})" if META[n]["short"] else n


def describe(name):
    func = DEFINITIONS[name].get("function") or {}
    params = list(((func.get("parameters") or {}).get("properties") or {}).keys())
    desc = " ".join(str(func.get("description") or "").split())
    first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0]
    return f"{name}({', '.join(params)}): {first}"


def args_fit(name, args):
    """True when the arguments carry every required parameter and no unknown one."""
    if not isinstance(args, dict) or name not in DEFINITIONS:
        return False
    params = (DEFINITIONS[name].get("function") or {}).get("parameters") or {}
    props = params.get("properties") or {}
    required = params.get("required") or []
    if any(r not in args for r in required):
        return False
    if props and any(k not in props for k in args):
        return False
    return True


def request_load(name):
    """Ask for a tool to join the session's list at the next turn. False when it is
    not available, already core, already loaded, or already pending."""
    if not available(name) or is_core(name) or is_loaded(name) or name in PENDING_LOADS:
        return False
    PENDING_LOADS.append(name)
    return True


_INJECTED_BLOCKS = re.compile(r"\n?\[(SYSTEM CONTEXT|AGENT IDENTITY FILE):")


def own_text(text):
    """The user's own words in a task message: everything before the blocks the agent
    appends (the system context, an inlined @identity file), which name every tool."""
    text = str(text or "")
    m = _INJECTED_BLOCKS.search(text)
    return (text[:m.start()] if m else text).rstrip()


def preload_from_text(text, limit=5):
    """Before a session's first request: tools the task names (the full tool name or an
    alias as a whole word) or clearly asks for (two or more distinct keywords of one
    tool). Only the user's own words count (`own_text`); a bare common word that happens
    to be a tool's name ("file", "goal", "think") is not enough on its own.
    Returns the names requested."""
    text = own_text(text).lower()
    words = _words(text)
    if not words:
        return []
    got = []
    for name in DEFINITIONS:
        if not available(name) or is_core(name) or is_loaded(name):
            continue
        hit = (re.search(r"(?<![a-z0-9_])" + re.escape(name.lower()) + r"(?![a-z0-9_])", text) is not None
               and "_" in name) or any(a.lower() in words for a in META[name]["aliases"])
        if not hit and len(words & set(META[name]["keywords"])) >= 2:
            hit = True
        if hit and request_load(name):
            got.append(name)
            if len(got) >= limit:
                break
    return got


def take_pending():
    """Drain PENDING_LOADS (the agent loop applies them to its session list)."""
    pending = [n for n in PENDING_LOADS if available(n)]
    del PENDING_LOADS[:]
    return pending


def mark_loaded(name):
    if name not in LOADED:
        LOADED.append(name)


def resume_pending():
    """Move a checkpoint's loaded tools into the pending list (once)."""
    for name in RESUME_LOADED:
        if available(name) and not is_core(name) and name not in LOADED and name not in PENDING_LOADS:
            PENDING_LOADS.append(name)
    del RESUME_LOADED[:]


def reset_session():
    """Forget the session's loads (tests, /clear)."""
    del LOADED[:]
    del PENDING_LOADS[:]
    del RESUME_LOADED[:]


_discover_tools()
