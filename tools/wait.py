"""wait: block until something the agent is tracking happens, and return all of it.

The fan-in half of non-blocking work. Start everything that can run (exec_command detaches
long commands, subagent with background=true, monitors), then `wait()` once instead of
polling: the process sits inside this tool call, no request is open, and the result is
everything that became ready, plus what is still running so the next step is informed.
Typed input ends the wait at once; esc·esc cancels it like any tool.
"""

import math

import monitor_bus

_MAX_WAIT_S = 6 * 3600
_short = "block until a tracked job, subagent or monitor has something"
_keywords = ["wait", "block", "finish", "done", "background", "session", "poll", "select"]
_aliases = ["wait_for", "await", "select"]


def _ids(val):
    if val is None:
        return []
    if isinstance(val, str):
        return [v.strip() for v in val.replace(",", " ").split() if v.strip()]
    if isinstance(val, (list, tuple)):
        return [str(v).strip() for v in val if str(v).strip()]
    return None


def fn(any_of=None, all_of=None, max_s: float = 600) -> str:
    """Block until something tracked happens; return everything that is ready.

    Args:
        any_of: source ids (session ids, subagent ids, monitor labels); return when one of
            them produces an item or finishes. Default: anything tracked.
        all_of: source ids; return only when every one of them has finished (a barrier).
        max_s: give up after this many seconds (default 600).
    """
    if isinstance(max_s, bool) or not isinstance(max_s, (int, float)) or not math.isfinite(max_s):
        return f"Error: 'max_s' must be a finite number, got {max_s!r}"
    if max_s < 0:
        return "Error: 'max_s' must be non-negative"
    max_s = min(float(max_s), _MAX_WAIT_S)
    any_ids, all_ids = _ids(any_of), _ids(all_of)
    if any_ids is None or all_ids is None:
        return "Error: 'any_of' and 'all_of' take a list of source ids"
    unknown = [i for i in any_ids + all_ids if monitor_bus.source(i) is None]
    if unknown:
        live = ", ".join(s.id for s in monitor_bus.list_sources()) or "none"
        return f"Error: unknown source(s) {', '.join(unknown)}; tracked: {live}"
    try:
        import cancel as _cancel
        cancel_check = _cancel.is_cancelled
    except ImportError:
        _cancel, cancel_check = None, None

    r = monitor_bus.wait(any_of=any_ids, all_of=all_ids, max_s=max_s, cancel_check=cancel_check)
    if r["reason"] == "cancelled" and _cancel is not None:
        raise _cancel.CancelledError()

    elapsed = r["elapsed"]
    n = len(r["items"])
    if r["interrupted"]:
        head = f"[wait] interrupted after {elapsed:.0f}s by {r['interrupted']}"
        head += f"; {n} item{'s' if n != 1 else ''} had arrived" if n else "; nothing had arrived"
    elif r["reason"] == "timeout":
        head = f"[wait] nothing happened in {elapsed:.0f}s"
    else:
        head = f"[wait] {n} item{'s' if n != 1 else ''} after {elapsed:.0f}s"
    body = "\n\n".join(r["items"])

    # Finished sessions and subagents are forgotten once their item has been returned
    # here; the exec_command session keeps the output readable regardless.
    for src in monitor_bus.list_sources():
        if src.done and src.kind in ("session", "subagent") and src.events:
            monitor_bus.forget(src.id)
    live = monitor_bus.list_sources(live_only=True)
    running = [s.describe() for s in live if s.kind in ("session", "subagent")]
    watching = [s.id for s in live if s.kind in ("monitor", "watch")]
    held = [f"{s.id}: {len(s.held)}" for s in live if s.held]
    foot = []
    if running:
        foot.append("still running: " + "; ".join(running))
    if watching:
        foot.append("watching: " + ", ".join(watching))
    if held:
        foot.append("held by the gate: " + ", ".join(held))
    if not running and not watching:
        foot.append("nothing else is tracked")
    out = head + ("\n" + body if body else "") + "\n(" + "; ".join(foot) + ")"
    return out


definition = {
    "type": "function",
    "function": {
        "name": "wait",
        "description": (
            "Block until something you are tracking happens, then return everything that is "
            "ready: a background exec session or subagent finishing, a monitor's output, a "
            "gated source waking you. Costs one request however long it waits, so start "
            "everything that can run first (long commands detach by themselves, "
            "subagent background=true, monitors) and call wait only when nothing else can "
            "proceed. With no arguments it returns on the first thing; any_of narrows to "
            "listed sources; all_of is a barrier (every listed source finished). Typed input "
            "interrupts it; call it again afterwards, nothing is lost. The result ends with "
            "what is still running."),
        "parameters": {
            "type": "object",
            "properties": {
                "any_of": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Source ids (session ids, subagent ids, monitor labels) to return on. Default: anything.",
                },
                "all_of": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Source ids that must all have finished before returning.",
                },
                "max_s": {
                    "type": "number",
                    "description": "Give up after this many seconds (default 600).",
                },
            },
            "required": [],
        },
    },
}
