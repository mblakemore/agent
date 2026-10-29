"""Recurring prompts for a long-running agent.

A *schedule* injects a fixed prompt into the agent loop every N minutes, the
same way a monitor ping does: an idle interactive session wakes and runs it.
Schedules are always saved (default `.agent/schedules.json`, or
AGENT_SCHEDULES_FILE) and re-armed by restore() at interactive start. If the
agent was down when one fell due, it fires once on restore, not once per missed
interval. The ticker is a daemon thread, so it never keeps the process alive.
"""

import json
import logging
import os
import threading
import time

import monitor_bus

_log = logging.getLogger("agent")

_TICK = 5.0            # seconds between due-checks
_MIN_MINUTES = 1.0
_MAX_SCHEDULES = 8
_MAX_PROMPT = 2000

_lock = threading.Lock()
_thread = None


def _path():
    return os.environ.get("AGENT_SCHEDULES_FILE") or os.path.join(".agent", "schedules.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(specs):
    path = _path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(specs, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        _log.warning("could not save schedules to %s: %s", path, e)


def fire_due(now=None):
    """Inject every schedule whose time has come; return the labels fired."""
    now = time.time() if now is None else now
    fired = []
    with _lock:
        specs = _load()
        for label, sp in specs.items():
            if now >= float(sp.get("next_due", 0)):
                monitor_bus.inject(f"[scheduled:{label}] {sp['prompt']}")
                sp["next_due"] = now + float(sp["every_minutes"]) * 60.0
                fired.append(label)
        if fired:
            _save(specs)
    return fired


def _run():
    while True:
        try:
            fire_due()
        except Exception:
            _log.debug("schedule tick failed", exc_info=True)
        time.sleep(_TICK)


def _ensure_thread():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_run, name="schedule-ticker", daemon=True)
        _thread.start()


def add(label, every_minutes, prompt, first_in_minutes=None):
    label = str(label or "").strip()
    prompt = str(prompt or "").strip()
    if not label:
        return {"ok": False, "error": "label required"}
    if not prompt:
        return {"ok": False, "error": "prompt required"}
    if len(prompt) > _MAX_PROMPT:
        return {"ok": False, "error": f"prompt too long (max {_MAX_PROMPT} chars)"}
    try:
        every = float(every_minutes)
    except (TypeError, ValueError):
        return {"ok": False, "error": "every_minutes must be a number"}
    if every < _MIN_MINUTES:
        return {"ok": False, "error": f"every_minutes must be at least {_MIN_MINUTES:g}"}
    first = every if first_in_minutes is None else max(0.0, float(first_in_minutes))
    with _lock:
        specs = _load()
        if label not in specs and len(specs) >= _MAX_SCHEDULES:
            return {"ok": False, "error": f"too many schedules (max {_MAX_SCHEDULES})"}
        replaced = label in specs
        specs[label] = {"every_minutes": every, "prompt": prompt,
                        "next_due": time.time() + first * 60.0}
        _save(specs)
    _ensure_thread()
    return {"ok": True, "label": label, "every_minutes": every, "replaced": replaced}


def remove(label):
    label = str(label or "").strip()
    with _lock:
        specs = _load()
        if specs.pop(label, None) is None:
            return {"ok": False, "error": f"no schedule '{label}'"}
        _save(specs)
    return {"ok": True, "label": label}


def list_all():
    now = time.time()
    return [{"label": k, "every_minutes": v.get("every_minutes"), "prompt": v.get("prompt"),
             "due_in_minutes": round(max(0.0, float(v.get("next_due", 0)) - now) / 60.0, 1)}
            for k, v in _load().items()]


def restore():
    """Start the ticker if any schedule is saved. Returns the labels found."""
    labels = list(_load())
    if labels:
        _ensure_thread()
    return labels
