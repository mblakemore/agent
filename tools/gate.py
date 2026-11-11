"""gate: tell the cheap judge what is worth waking you for, per source.

Exists only when `preferences.gate.enabled` is true (agent.py gates it). A source with a
brief has its output judged by the summary model before it reaches you: "wake" comes
through now, "hold" is kept and shown later as a digest (on the next wake, after
`digest_after_s`, or when the held buffer fills). `bypass` and `quiet` are regexes
checked before any model call. Nothing is ever dropped.
"""

import monitor_bus

_tier = "hidden"       # deferred (named in the catalog) once preferences.gate.enabled is set
_short = "set what a background source may wake you for"
_keywords = ["gate", "brief", "filter", "digest", "hold", "wake", "noise"]
_MAX_BRIEF = 1500


def fn(action: str = "status", source: str = "", brief: str = None, bypass: str = None,
       quiet: str = None) -> str:
    action = str(action or "status").strip().lower()
    if not monitor_bus.gate_enabled():
        return "Error: the gate is off (preferences.gate.enabled is false)"

    if action == "status":
        srcs = monitor_bus.list_sources()
        if not srcs:
            return "No sources are tracked."
        cfg = monitor_bus.gate_config()
        lines = [f"Gate: digests after {cfg['digest_after_s']:g}s or {cfg['max_held_chars']} "
                 f"held chars; judge timeout {cfg['timeout_s']:g}s."]
        for s in srcs:
            lines.append("- " + s.describe())
            if s.brief:
                lines.append(f"    brief: {s.brief[:200]}")
            if s.bypass_src or s.quiet_src:
                lines.append(f"    bypass: {s.bypass_src or '-'}   quiet: {s.quiet_src or '-'}")
        return "\n".join(lines)

    if action == "flush":
        n = monitor_bus.flush_held(source or None)
        return (f"Flushed held items of {n} source{'s' if n != 1 else ''} onto the bus; "
                "they arrive at your next boundary." if n else "Nothing was held.")

    if action in ("set", "off"):
        if not str(source or "").strip():
            return "Error: 'source' is required (a monitor label, session id or subagent id)"
        if action == "off":
            r = monitor_bus.set_gate(source, brief="", bypass="", quiet="")
            if not r.get("ok"):
                return f"Error: {r.get('error')}"
            monitor_bus.flush_held(source)
            return f"Source '{source}' is no longer gated; anything held was flushed."
        if brief is not None and len(str(brief)) > _MAX_BRIEF:
            return f"Error: brief too long (max {_MAX_BRIEF} chars)"
        r = monitor_bus.set_gate(source, brief=brief, bypass=bypass, quiet=quiet)
        if not r.get("ok"):
            return f"Error: {r.get('error')}"
        return (f"Source '{source}' {'gated' if r.get('gated') else 'not gated (no brief or patterns)'}: "
                "its output is judged against the brief before it reaches you; held output "
                "comes as a digest later." if r.get("gated") else
                f"Source '{source}' has no brief or patterns, so nothing is gated.")

    return f"Error: unknown action '{action}' (use status|set|flush|off)."


definition = {
    "type": "function",
    "function": {
        "name": "gate",
        "description": (
            "Control the gate in front of background sources (monitors, watched files, "
            "sessions): set a brief saying what is worth waking you for, and the cheap judge "
            "holds the rest for a digest. bypass/quiet are regexes that wake or hold without "
            "a judge call. status lists sources and what is held; flush delivers held items "
            "now; off removes a source's gate."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "set", "flush", "off"],
                           "description": "Default status."},
                "source": {"type": "string",
                           "description": "A monitor label, session id or subagent id (set/off/flush)."},
                "brief": {"type": "string",
                          "description": "What is worth a wake, in your own words; empty keeps the current brief."},
                "bypass": {"type": "string",
                           "description": "Regex: matching output always wakes you, no judge call."},
                "quiet": {"type": "string",
                          "description": "Regex: matching output is always held, no judge call."},
            },
            "required": [],
        },
    },
}
