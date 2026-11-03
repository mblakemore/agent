"""switch_model: change the main or summary model at runtime, from a tool call.

The same switch `/model ROLE NAME` makes from the input line: the live backend changes in
place, the choice is persisted to `.agent/config.json`, and the next request goes to the
new model (with a cold prompt cache once; the history is kept, another model's thinking
blocks are dropped on the wire). Only the models in `preferences.switchable_models` can
be chosen; with none configured the tool does not exist to the model (agent.py gates it).

The harness exposes the switch through `tools.RUNTIME` (agent.py fills it); under a
harness that does not, the tool answers with an error instead of raising.
"""

import tools as _registry

_tier = "hidden"       # deferred (named in the catalog) once preferences.switchable_models is set
_short = "switch the model at runtime"
_keywords = ["model", "switch", "change", "llm", "effort"]


def fn(model: str = "", role: str = "main") -> str:
    rt = _registry.RUNTIME
    get_model, set_model, models = rt.get("get_model"), rt.get("set_model"), rt.get("models")
    if not (callable(get_model) and callable(set_model) and callable(models)):
        return "Error: this harness exposes no runtime model control (tools.RUNTIME is empty)"
    role = str(role or "main").strip().lower()
    try:
        allowed = [str(m) for m in models(role)]
        current = str(get_model(role) or "")
    except Exception as e:  # noqa: BLE001 — an unknown role, or a harness without config
        return f"Error: {e}"
    model = str(model or "").strip()
    if not model:
        return (f"{role} model: {current or 'unknown'}; switchable: "
                f"{', '.join(allowed) if allowed else 'none (preferences.switchable_models is empty)'}")
    if model not in allowed:
        return (f"Error: {model!r} is not a switchable {role} model "
                f"({', '.join(allowed) if allowed else 'none'}); the list is "
                "preferences.switchable_models in .agent/config.json")
    if model == current:
        return f"{role} model is already {model}; nothing changed"
    try:
        set_model(role, model)
    except Exception as e:  # noqa: BLE001 — report, never crash the turn
        return f"Error: switch to {model} failed: {e}"
    return (f"{role} model switched to {model} (was {current or 'unknown'}) and persisted to "
            ".agent/config.json; the next request goes to it, with a cold prompt cache once.")


definition = {
    "type": "function",
    "function": {
        "name": "switch_model",
        "description": (
            "Switch the main (or summary) model at runtime to one of the configured "
            "switchable models; persists like /model, the next request uses it. With no "
            "model: report the current model and the switchable list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "A model from preferences.switchable_models; empty to report.",
                },
                "role": {
                    "type": "string",
                    "enum": ["main", "summary"],
                    "description": "Which backend to switch (default main).",
                },
            },
            "required": [],
        },
    },
}
