"""What the loop is doing right now, for the operator.

The turn loop records its state here (the turn, the request in flight, the tool call in
flight with its full arguments, the last reply, the last tool result) and the status key
reads it back as text. The record is never model input: it exists so the person at the
keyboard can see past a truncated tool line without asking the agent anything.
"""
import json
import threading
import time

_LOCK = threading.Lock()
STATE = {
    "turn": 0,
    "phase": "idle",          # idle | request | tool | between
    "since": None,            # when the phase began (monotonic)
    "model": None,            # the model the request in flight went to
    "tool": None,             # the tool call in flight: name
    "tool_args": None,        # ... and its full arguments
    "last_text": "",          # the last assistant text
    "last_text_at": None,
    "last_tool": None,        # (name, result chars, finished at)
    "calls": 0,               # tool calls this session
}
ARGS_MAX_CHARS = 20000        # the full arguments, within reason


def _set(**kw):
    with _LOCK:
        STATE.update(kw)


def turn_started(turn):
    _set(turn=turn, phase="between", since=time.monotonic())


def request_started(model=None):
    _set(phase="request", since=time.monotonic(), model=model, tool=None, tool_args=None)


def response_text(text):
    _set(last_text=text or "", last_text_at=time.monotonic(), phase="between", since=time.monotonic())


def tool_started(name, args):
    with _LOCK:
        STATE.update(phase="tool", since=time.monotonic(), tool=name, tool_args=args)
        STATE["calls"] += 1


def tool_finished(name, result_chars):
    _set(phase="between", since=time.monotonic(), tool=None, tool_args=None,
         last_tool=(name, int(result_chars), time.monotonic()))


def idle():
    _set(phase="idle", since=time.monotonic(), tool=None, tool_args=None)


def snapshot():
    with _LOCK:
        return dict(STATE)


def reset():
    with _LOCK:
        STATE.update(turn=0, phase="idle", since=None, model=None, tool=None, tool_args=None,
                     last_text="", last_text_at=None, last_tool=None, calls=0)


def _ago(t, now):
    if t is None:
        return "?"
    s = max(0.0, now - t)
    if s < 60:
        return "%.0fs" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def _args_text(args):
    try:
        text = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001
        text = repr(args)
    if len(text) > ARGS_MAX_CHARS:
        text = text[:ARGS_MAX_CHARS] + "\n… (%d more chars)" % (len(text) - ARGS_MAX_CHARS)
    return text


def describe(now=None):
    """The record as lines: the phase and how long, the tool call in flight in full, the
    last reply and the last tool result."""
    s = snapshot()
    now = time.monotonic() if now is None else now
    lines = []
    head = "turn %d · " % s["turn"]
    if s["phase"] == "tool":
        head += "%s running for %s (call %d this session)" % (s["tool"], _ago(s["since"], now), s["calls"])
    elif s["phase"] == "request":
        head += "waiting on %s for %s" % (s["model"] or "the model", _ago(s["since"], now))
    elif s["phase"] == "between":
        head += "between steps for %s" % _ago(s["since"], now)
    else:
        head += "idle, waiting for input" + ("" if s["since"] is None else " for %s" % _ago(s["since"], now))
    lines.append(head)
    if s["phase"] == "tool":
        lines.append("arguments:")
        lines.extend("  " + l for l in _args_text(s["tool_args"]).split("\n"))
    if s["last_tool"]:
        name, chars, at = s["last_tool"]
        lines.append("last tool result: %s, %s chars, %s ago" % (name, "{:,}".format(chars), _ago(at, now)))
    if s["last_text"]:
        text = " ".join(s["last_text"].split())
        if len(text) > 300:
            text = text[:300] + "…"
        lines.append("last said (%s ago): %s" % (_ago(s["last_text_at"], now), text))
    return "\n".join(lines)
