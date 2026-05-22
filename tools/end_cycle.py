"""End-cycle tool — lets the agent signal clean cycle completion.

Marked _auto_exclude=True so it is registered in MAP_FN (dispatchable) but NOT
added to the global tools schema list on startup.  agent.py unlocks it into the
per-session tools list after the first nudge fires, giving the agent a clean
exit path without letting it skip the cycle entirely by calling end_cycle before
doing any real work.
"""

# Sentinel returned by fn(); agent.py intercepts this value and returns "done".
SENTINEL = "__END_CYCLE_REQUESTED__"

# Excluded from auto-discovery schema list; tools/__init__.py checks this flag.
_auto_exclude = True


def fn(summary: str = "") -> str:
    """Signal that this cycle is complete and the agent should exit cleanly.

    Args:
        summary: One sentence describing what was accomplished this cycle.
    """
    return SENTINEL


definition = {
    "type": "function",
    "function": {
        "name": "end_cycle",
        "description": (
            "Signal clean cycle completion and exit. Call this when all planned "
            "work for this cycle is done and committed to git. Provide a one-sentence "
            "summary of what was accomplished. Do NOT call this before committing — "
            "use git commit + push first, then call end_cycle."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One sentence describing what was accomplished this cycle.",
                }
            },
            "required": ["summary"],
        },
    },
}
