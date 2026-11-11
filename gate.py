"""The gate: a cheap judge in front of a bus source, and the digest of what it held.

A source (a monitor, a watched file, a background session) may carry a *brief*: prose the
model wrote saying what is worth waking it for. Output from a source with a brief goes
through `judge()` before it reaches the bus:

  1. the source's `bypass` regex: a match wakes at once, no model call;
  2. its `quiet` regex: a match is held at once, no model call;
  3. otherwise one small call to the summary model with the brief, the lines already held
     and the new text, answered as JSON {"verdict": "wake"|"hold", "why", "line"}.

Any failure (no judge configured, a timeout, an answer that is not JSON, an unknown
verdict) is "wake": the gate fails open, so a broken judge costs money, never an event.
"Drop" is not a verdict. Held items flush as one digest (see monitor_bus) when they age
past `digest_after_s`, grow past `max_held_chars`, the source next wakes, or the agent
wakes for any other reason.

Pure functions over plain data: no threads, no bus; monitor_bus owns the state.
"""

import json
import logging
import os
import re
import time

_log = logging.getLogger("agent")

WAKE, HOLD = "wake", "hold"

DEFAULTS = {
    "enabled": False,
    "timeout_s": 20.0,
    "digest_after_s": 600.0,
    "max_held_chars": 8000,
    "digest_raw_chars": 0,
    "log_dir": os.path.join(".agent", "gate"),
}

_PROMPT_TEXT_CAP = 3000      # chars of new output shown to the judge
_HELD_IN_PROMPT = 12         # most recent held lines shown to the judge
_MAX_TOKENS = 200


class Verdict:
    __slots__ = ("verdict", "why", "line", "how", "ms")

    def __init__(self, verdict, why="", line="", how="judge", ms=0):
        self.verdict = verdict
        self.why = why
        self.line = line
        self.how = how          # bypass | quiet | judge | failopen
        self.ms = ms

    def __repr__(self):
        return f"Verdict({self.verdict}, {self.how}, {self.why!r})"


def config_from(raw):
    """A full gate config from a `preferences.gate` dict (anything missing is a default)."""
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw and raw[k] is not None:
                cfg[k] = raw[k]
    cfg["enabled"] = bool(cfg["enabled"])
    for k in ("timeout_s", "digest_after_s"):
        try:
            cfg[k] = max(0.0, float(cfg[k]))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    for k in ("max_held_chars", "digest_raw_chars"):
        try:
            cfg[k] = max(0, int(cfg[k]))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    cfg["log_dir"] = str(cfg["log_dir"] or DEFAULTS["log_dir"])
    return cfg


def compile_pattern(pattern):
    """A compiled regex, or None for an empty or invalid pattern (invalid is logged)."""
    pattern = str(pattern or "").strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        _log.warning("gate: ignoring invalid pattern %r: %s", pattern, e)
        return None


def first_line(text, limit=160):
    line = " ".join(str(text or "").strip().split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def build_prompt(brief, label, held_lines, text):
    held = "\n".join(f"- {h}" for h in held_lines[-_HELD_IN_PROMPT:]) or "- (nothing)"
    if len(text) > _PROMPT_TEXT_CAP:
        text = text[:_PROMPT_TEXT_CAP] + "\n…[truncated]"
    return (
        "You are a gate in front of an agent that is busy or idle. Decide whether this new "
        "output needs the agent's attention now (\"wake\") or can be held for a later "
        "digest (\"hold\"). Nothing is ever dropped: held output is shown later.\n\n"
        f"The agent's brief, what it wants to be woken for:\n{brief.strip()}\n\n"
        f"Already held from this source and not yet shown:\n{held}\n\n"
        f"New output from source '{label}':\n{text}\n\n"
        "Answer with one JSON object and nothing else: "
        '{"verdict": "wake" or "hold", "why": "<one short reason>", '
        '"line": "<one-line summary of the new output>"}'
    )


def parse_answer(answer):
    """(verdict, why, line) from the judge's text; None when it is not a usable answer."""
    s = str(answer or "")
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in (WAKE, HOLD):
        return None
    return verdict, first_line(obj.get("why", ""), 200), first_line(obj.get("line", ""))


def judge(brief, label, text, held_lines, bypass=None, quiet=None, complete=None,
          timeout=20.0):
    """Decide wake or hold for `text` from source `label`.

    `bypass` / `quiet` are compiled regexes or None; `complete(prompt, timeout) -> str`
    is the summary-model call (None means no judge: wake). Never raises."""
    if bypass is not None and bypass.search(text):
        return Verdict(WAKE, "matched bypass", first_line(text), how="bypass")
    if quiet is not None and quiet.search(text):
        return Verdict(HOLD, "matched quiet", first_line(text), how="quiet")
    if complete is None or not str(brief or "").strip():
        return Verdict(WAKE, "no judge or no brief", first_line(text), how="failopen")
    t0 = time.monotonic()
    try:
        answer = complete(build_prompt(brief, label, held_lines, text), timeout)
    except Exception as e:  # noqa: BLE001 — the gate fails open on any judge error
        ms = int((time.monotonic() - t0) * 1000)
        _log.info("gate.verdict source=%s verdict=wake how=failopen ms=%d err=%s",
                  label, ms, type(e).__name__)
        return Verdict(WAKE, f"judge failed: {type(e).__name__}", first_line(text),
                       how="failopen", ms=ms)
    ms = int((time.monotonic() - t0) * 1000)
    parsed = parse_answer(answer)
    if parsed is None:
        _log.info("gate.verdict source=%s verdict=wake how=failopen ms=%d err=unparseable",
                  label, ms)
        return Verdict(WAKE, "judge answer unreadable", first_line(text), how="failopen", ms=ms)
    verdict, why, line = parsed
    _log.info("gate.verdict source=%s verdict=%s how=judge ms=%d why=%s", label, verdict, ms, why)
    return Verdict(verdict, why, line or first_line(text), how="judge", ms=ms)


def format_digest(label, held, raw_chars=0, log_path=None, reason=""):
    """One bus item summarising held entries: [(ts, line, raw_text), ...]."""
    if not held:
        return ""
    n = len(held)
    since = time.strftime("%H:%M", time.localtime(held[0][0]))
    head = f"[digest:{label}] {n} held item{'s' if n != 1 else ''} since {since}"
    if reason:
        head += f" ({reason})"
    if log_path:
        head += f"; raw text: {log_path}"
    lines = [head]
    for ts, line, _raw in held:
        lines.append(f"- {time.strftime('%H:%M', time.localtime(ts))} {line}")
    if raw_chars > 0:
        raw = "\n".join(r for _ts, _line, r in held if r)
        if len(raw) > raw_chars:
            raw = raw[-raw_chars:]
            raw = "…" + raw
        if raw:
            lines.append("raw:\n" + raw)
    return "\n".join(lines)


def append_raw(log_dir, label, text):
    """Keep the raw held text on disk so the agent can read it if the digest is too thin."""
    try:
        os.makedirs(log_dir, exist_ok=True)
        gi = os.path.join(log_dir, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as fh:
                fh.write("*\n")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label))[:60] or "source"
        path = os.path.join(log_dir, f"{safe}.log")
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip("\n") + "\n")
        return path
    except OSError:
        return None
