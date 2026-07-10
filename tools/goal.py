"""Goal stack tool — durable multi-cycle goals via state/goals.json (WS2).

The missing cross-cycle planning primitive: task_tracker is per-session;
this survives cycles. A goal links to an optional plan file (validated by
make_plan's playbook linter) and carries a step DAG. Each PERCEIVE, agent.py
hydrates "goal → next actionable step" into the preamble so multi-cycle work
resumes without re-derivation from git log (the rediscovery-loop fix).

Storage: ``state/goals.json`` relative to the working directory (same
convention as the creatures' state/ layout). Pure-additive: absent file =
feature off.
"""

import json
import os
import time

_GOALS_FILE = os.path.join("state", "goals.json")
_PREAMBLE_CHAR_CAP = 2400  # ≈600 tokens — 64K-window consumers (WS2 brief)


def _load():
    try:
        with open(_GOALS_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
            return {"goals": []}
        return data
    except FileNotFoundError:
        return {"goals": []}
    except Exception:
        return {"goals": []}


def _save(data):
    os.makedirs(os.path.dirname(_GOALS_FILE) or ".", exist_ok=True)
    tmp = _GOALS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _GOALS_FILE)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _find(data, goal_id):
    for g in data["goals"]:
        if g.get("id") == goal_id:
            return g
    return None


def _next_step(goal):
    for s in goal.get("steps", []):
        if s.get("status") == "pending":
            return s
    return None


def _fmt_goal(g, verbose=False):
    steps = g.get("steps", [])
    done = sum(1 for s in steps if s.get("status") == "done")
    line = (f"[{g['id']}] {g['title']} — {g.get('status', 'active')} "
            f"({done}/{len(steps)} steps)")
    if g.get("plan_path"):
        line += f" plan={g['plan_path']}"
    nxt = _next_step(g)
    if nxt:
        line += f"\n  next: ({nxt['id']}) {nxt['description']}"
    if verbose:
        for s in steps:
            mark = {"done": "x", "pending": " "}.get(s.get("status"), "?")
            line += f"\n  [{mark}] ({s['id']}) {s['description']}"
            if s.get("outcome"):
                line += f" → {s['outcome']}"
    return line


def preamble_summary():
    """Called by agent.py at session start. Returns '' when nothing active."""
    data = _load()
    active = [g for g in data["goals"] if g.get("status") == "active"]
    if not active:
        return ""
    lines = ["GOAL STACK (state/goals.json — durable across cycles; "
             "advance with goal(action='step-done', ...)):"]
    for g in active:
        lines.append(_fmt_goal(g))
    out = "\n".join(lines)
    if len(out) > _PREAMBLE_CHAR_CAP:
        out = out[:_PREAMBLE_CHAR_CAP] + "\n  … (truncated — run goal(action='list'))"
    return out


def _validate_plan(plan_path):
    """Lint a plan file via make_plan's playbook validator when available.
    Returns (ok, detail)."""
    if not os.path.exists(plan_path):
        return False, f"plan file not found: {plan_path}"
    try:
        from tools import make_plan
        result = make_plan.fn(action="validate", path=plan_path)
        ok = "FAIL" not in str(result)[:200].upper()
        return ok, str(result)[:400]
    except Exception as e:
        # Validator unavailable — accept the plan, note the gap.
        return True, f"(playbook validator unavailable: {e})"


def fn(action, title=None, goal_id=None, plan_path=None, description=None,
       step_id=None, outcome=None, status=None):
    """Manage the durable goal stack.

    Actions:
      add        — new goal (title required; plan_path optional)
      plan       — attach + validate a plan file (goal_id, plan_path)
      step-add   — append a step (goal_id, description)
      step-done  — complete a step (goal_id, step_id; outcome optional)
      status     — one goal in detail (goal_id)
      list       — all goals, active first
      complete   — mark goal done (goal_id; status='abandoned' to drop)
    """
    data = _load()

    if action == "add":
        if not title:
            return "Error: 'title' is required for add"
        gid = f"g{len(data['goals']) + 1:03d}"
        goal = {"id": gid, "title": title, "status": "active",
                "plan_path": None, "steps": [],
                "created": _now(), "updated": _now()}
        if plan_path:
            ok, detail = _validate_plan(plan_path)
            if not ok:
                return (f"Error: plan validation failed — goal NOT created. "
                        f"{detail}")
            goal["plan_path"] = plan_path
        data["goals"].append(goal)
        _save(data)
        return f"Goal {gid} created: {title}" + (
            f" (plan {plan_path} attached)" if goal["plan_path"] else
            ". Attach a validated plan before multi-cycle execution: "
            "goal(action='plan', goal_id=..., plan_path=...)")

    if action == "list":
        if not data["goals"]:
            return "No goals. Create one: goal(action='add', title='...')"
        ordered = sorted(data["goals"],
                         key=lambda g: g.get("status") != "active")
        return "\n".join(_fmt_goal(g) for g in ordered)

    # Remaining actions need a goal.
    goal = _find(data, goal_id) if goal_id else None
    if goal is None:
        return f"Error: goal_id {goal_id!r} not found (use goal(action='list'))"

    if action == "plan":
        if not plan_path:
            return "Error: 'plan_path' is required for plan"
        ok, detail = _validate_plan(plan_path)
        if not ok:
            return f"Error: plan validation failed — not attached. {detail}"
        goal["plan_path"] = plan_path
        goal["updated"] = _now()
        _save(data)
        return f"Plan {plan_path} validated and attached to {goal['id']}. {detail}"

    if action == "step-add":
        if not description:
            return "Error: 'description' is required for step-add"
        sid = f"s{len(goal['steps']) + 1:02d}"
        goal["steps"].append({"id": sid, "description": description,
                              "status": "pending", "outcome": None})
        goal["updated"] = _now()
        _save(data)
        return f"Step {sid} added to {goal['id']}: {description}"

    if action == "step-done":
        step = next((s for s in goal["steps"] if s.get("id") == step_id), None)
        if step is None:
            return f"Error: step_id {step_id!r} not found in {goal['id']}"
        step["status"] = "done"
        if outcome:
            step["outcome"] = outcome
        goal["updated"] = _now()
        remaining = _next_step(goal)
        if remaining is None and goal["steps"]:
            goal["status"] = "done"
        _save(data)
        if goal["status"] == "done":
            return (f"Step {step_id} done — all steps complete, goal "
                    f"{goal['id']} marked done.")
        return (f"Step {step_id} done." +
                (f" Next: ({remaining['id']}) {remaining['description']}"
                 if remaining else ""))

    if action == "status":
        return _fmt_goal(goal, verbose=True)

    if action == "complete":
        goal["status"] = status if status in ("done", "abandoned") else "done"
        goal["updated"] = _now()
        _save(data)
        return f"Goal {goal['id']} marked {goal['status']}."

    return (f"Error: unknown action {action!r}. Valid: add, plan, step-add, "
            "step-done, status, list, complete")


definition = {
    "type": "function",
    "function": {
        "name": "goal",
        "description": (
            "Durable multi-cycle goal stack (state/goals.json). Unlike "
            "task_tracker (per-session), goals persist across cycles and are "
            "hydrated into the PERCEIVE preamble as 'goal → next actionable "
            "step'. Attach a plan file (playbook-validated) before executing "
            "a goal that spans multiple cycles."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["add", "plan", "step-add", "step-done",
                                    "status", "list", "complete"]},
                "title": {"type": "string", "description": "Goal title (add)"},
                "goal_id": {"type": "string", "description": "Goal id, e.g. g001"},
                "plan_path": {"type": "string",
                              "description": "Plan markdown path (add/plan)"},
                "description": {"type": "string",
                                "description": "Step description (step-add)"},
                "step_id": {"type": "string", "description": "Step id (step-done)"},
                "outcome": {"type": "string",
                            "description": "Observed outcome (step-done)"},
                "status": {"type": "string", "enum": ["done", "abandoned"],
                           "description": "Terminal status (complete)"},
            },
            "required": ["action"],
        },
    },
}
