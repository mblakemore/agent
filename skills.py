"""Skills: instruction files the model pulls into its context by name.

A skill is a markdown file of instructions for one kind of task (joining a meeting,
releasing a package, the house style for a report). It is text in a message, not a
tool schema: loaded mid-session it is appended, costs no cache miss, and leaves the
window by trimming like any other message. Three ways in:

  - the model: `tool_search skill:NAME` answers with the skill's text as the tool result;
  - the keyboard: `/skill NAME` queues it as a user message for the next turn;
  - the picker (`skills.pick`): the summary model reads the task and the catalog and
    loads a skill above `load_threshold`, or suggests one above `suggest_threshold`.

Discovery (`discover()`): every `SKILL.md` under `.agent/skills/<name>/`, then under
`.claude/skills/<name>/` (Claude Code's layout, so a project's skills serve both
harnesses unchanged), then each directory in `skills.dirs`, then the documents the
`skills.catalog` config list registers. Frontmatter read: `name` (defaults to the
directory name), `description` (required), `keywords` (this harness's addition: whole-
word phrases that load the skill at session start when the picker is off or fails) and
`disable-model-invocation` (true keeps it out of the catalog and off the picker).

State: `SKILLS` (name -> Skill, discovery order), `LOADED` (names whose text is in the
window, re-derived from the window each turn by `sync_loaded`), `PENDING` (names to add
as a user message at the next turn boundary), `SUGGEST` (names for one line in the
runtime note). `reset_session()` clears the session state (tests, /clear).

The picker fails open like the gate (gate.py): a timeout, an error or an unreadable
answer loads nothing and logs why. Every verdict is one `skill.pick` log line and one
telemetry event; every load is one `skill.load` line.
"""

import json
import logging
import os
import re
import threading
import time

_log = logging.getLogger("agent")

DEFAULTS = {
    "dirs": [],                 # extra directories of <name>/SKILL.md
    "catalog": [],              # registered documents: {name, path, description, keywords?}
    "max_chars": 20000,         # a bigger file is not a skill (and must never spill)
    "max_loaded_tokens": 12000, # loads past this are refused with a notice
    "pick": "off",              # "start" | "always"
    "pick_on_events": False,    # "always" also judges delivered bus items
    "load_threshold": 0.75,
    "suggest_threshold": 0.4,
    "max_picks_per_turn": 2,
    "pick_timeout_s": 15,
}
PICK_LEVELS = ("off", "start", "always")

CATALOG_PREFIX = "Skills, loaded on request with tool_search skill:NAME:"
HEADER_PREFIX = "[skill: "
_HEADER_RE = re.compile(r"^\[skill: ([^\s()]+) \(")
_HEADER_LINE_RE = re.compile(r"^\[skill: ([^\s()]+) \(", re.MULTILINE)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CATALOG_DESC_CHARS = 60
_PICK_TEXT_CHARS = 2000
_PICK_DESC_CHARS = 300
_PICK_MAX_TOKENS = 300

INSTRUCTIONS = (
    "Skills: instruction files for particular kinds of task, named with a short "
    "description in the line that begins \"Skills, loaded on request\". "
    "Load one with tool_search skill:NAME (several: skill:NAME,NAME); its text arrives "
    "as the tool result. A loaded skill is ordinary context: it may be summarized away "
    "later, and can be loaded again. When a skill's description matches what you are "
    "about to do, load it before starting."
)

CFG = dict(DEFAULTS)
SKILLS = {}        # name -> Skill, in discovery order
LOADED = []        # names whose text is in the window, in order of appearance
PENDING = []       # [(name, by)] to add as a user message at the next turn boundary
SUGGEST = []       # names to suggest in the runtime note at the next turn boundary
RESUME_LOADED = [] # names a checkpoint restored (until the first sync against the window)
STATE = {"started": False}   # the session-start pick ran
_complete = None   # complete(prompt, timeout) -> str: the summary model
_count_tokens = None
_cwd = None


class Skill:
    __slots__ = ("name", "path", "dir", "description", "keywords", "hidden", "body",
                 "registered", "tokens")

    def __init__(self, name, path, description, body, keywords=(), hidden=False, registered=False):
        self.name = name
        self.path = path
        self.dir = os.path.dirname(path)
        self.description = description
        self.keywords = [str(k).strip().lower() for k in keywords if str(k).strip()]
        self.hidden = bool(hidden)
        self.body = body
        self.registered = registered
        self.tokens = _tokens(self.render())

    @property
    def lines(self):
        return self.body.count("\n") + (1 if self.body and not self.body.endswith("\n") else 0)

    def header(self):
        rel = _display_path(self.path)
        n = f"{self.lines} line" + ("" if self.lines == 1 else "s")
        if self.registered:
            return f"[skill: {self.name} ({rel}), {n}]"
        return (f"[skill: {self.name} ({rel}), {n} — files beside it "
                f"resolve against {_display_path(self.dir)}]")

    def render(self):
        return self.header() + "\n" + self.body.rstrip("\n") + "\n"

    def short(self):
        return _first_clause(self.description, _CATALOG_DESC_CHARS)


# ── Configuration and discovery ──────────────────────────────────────

def configure(raw, cwd=None, complete=None, count_tokens=None, spill_cap=None):
    """Apply the `skills` config block (anything missing is a default). `complete` is
    the summary-model call the picker uses; `count_tokens(text)` the estimator;
    `spill_cap` the harness's tool-result spill limit, which `max_chars` never exceeds
    (a skill load must never spill)."""
    global _complete, _count_tokens, _cwd
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw and raw[k] is not None:
                cfg[k] = raw[k]
    cfg["dirs"] = [str(d) for d in cfg["dirs"] if isinstance(d, str) and d.strip()] \
        if isinstance(cfg["dirs"], list) else []
    cfg["catalog"] = [e for e in cfg["catalog"] if isinstance(e, dict)] \
        if isinstance(cfg["catalog"], list) else []
    for k in ("max_chars", "max_loaded_tokens", "max_picks_per_turn"):
        try:
            cfg[k] = max(0, int(cfg[k]))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    for k in ("load_threshold", "suggest_threshold", "pick_timeout_s"):
        try:
            cfg[k] = max(0.0, float(cfg[k]))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    pick = str(cfg["pick"] or "off").strip().lower()
    if pick in ("true", "on"):
        pick = "always"
    if pick not in PICK_LEVELS:
        _log.warning("skills.pick: unknown level %r (off, start or always); using off", cfg["pick"])
        pick = "off"
    cfg["pick"] = pick
    cfg["pick_on_events"] = bool(cfg["pick_on_events"])
    if spill_cap and cfg["max_chars"] > int(spill_cap):
        _log.warning("skills.max_chars %d is over limits.tool_result_max_chars %d; a skill load "
                     "must never spill, so %d is used", cfg["max_chars"], int(spill_cap), int(spill_cap))
        cfg["max_chars"] = int(spill_cap)
    CFG.clear()
    CFG.update(cfg)
    _cwd = cwd or os.getcwd()
    if complete is not None:
        _complete = complete
    if count_tokens is not None:
        _count_tokens = count_tokens
    return dict(CFG)


def set_judge(complete):
    global _complete
    _complete = complete


def discover(cwd=None, log=None):
    """Find the skills: the two conventional directories, `skills.dirs`, then the
    registered documents. Returns the names found, in order; problems are warnings."""
    global _cwd
    log = log or _log
    _cwd = cwd or _cwd or os.getcwd()
    SKILLS.clear()
    roots = [os.path.join(_cwd, ".agent", "skills"), os.path.join(_cwd, ".claude", "skills")]
    roots += [d if os.path.isabs(d) else os.path.join(_cwd, d) for d in CFG["dirs"]]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            subdirs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
        except OSError as e:
            log.warning("skills: cannot list %s: %s", root, e)
            continue
        for d in subdirs:
            path = os.path.join(root, d, "SKILL.md")
            if os.path.isfile(path):
                _add(_from_skill_file(path, d, log), log)
    for entry in CFG["catalog"]:
        _add(_from_registered(entry, log), log)
    if SKILLS:
        log.info("skills.discovered %d: %s", len(SKILLS), ", ".join(SKILLS))
    return list(SKILLS)


def _add(skill, log):
    if skill is None:
        return
    if skill.name in SKILLS:
        log.warning("skills: %s is already a skill (%s); %s ignored",
                    skill.name, _display_path(SKILLS[skill.name].path), _display_path(skill.path))
        return
    SKILLS[skill.name] = skill


def _read(path, log):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as e:
        log.warning("skills: cannot read %s: %s", _display_path(path), e)
        return None


def _from_skill_file(path, dirname, log):
    text = _read(path, log)
    if text is None:
        return None
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or dirname).strip()
    description = " ".join(str(meta.get("description") or "").split())
    if not description:
        log.warning("skills: %s has no description in its frontmatter; skipped", _display_path(path))
        return None
    keywords = meta.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k for k in re.split(r"[,\n]", keywords)]
    hidden = str(meta.get("disable-model-invocation", "")).strip().lower() in ("true", "yes", "1")
    return _checked(Skill(name, path, description, body, keywords, hidden), log)


def _from_registered(entry, log):
    name = str(entry.get("name") or "").strip()
    path = str(entry.get("path") or "").strip()
    description = " ".join(str(entry.get("description") or "").split())
    if not (name and path and description):
        log.warning("skills.catalog: an entry needs name, path and description; skipped: %r",
                    {k: entry.get(k) for k in ("name", "path")})
        return None
    full = path if os.path.isabs(path) else os.path.join(_cwd or os.getcwd(), path)
    text = _read(full, log)
    if text is None:
        return None
    _meta, body = parse_frontmatter(text)
    keywords = entry.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    hidden = bool(entry.get("disable-model-invocation", False))
    return _checked(Skill(name, full, description, body, keywords, hidden, registered=True), log)


def _checked(skill, log):
    if not _NAME_RE.match(skill.name):
        log.warning("skills: %r is not a usable skill name (letters, digits, _ . -); %s skipped",
                    skill.name, _display_path(skill.path))
        return None
    if not skill.body.strip():
        log.warning("skills: %s is empty; skipped", _display_path(skill.path))
        return None
    if len(skill.render()) > CFG["max_chars"]:
        log.warning("skills: %s is %d chars, over skills.max_chars (%d): a skill is a few "
                    "thousand tokens of instructions, not a manual; skipped",
                    _display_path(skill.path), len(skill.render()), CFG["max_chars"])
        return None
    return skill


_FRONT_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)


def parse_frontmatter(text):
    """(fields, body) from a markdown file with optional YAML frontmatter. Enough YAML
    for a skill file: `key: value`, `key: [a, b]`, quoted strings, and block lists
    (`key:` then `- item` lines). Unknown or odd fields are kept as strings or ignored;
    a file without frontmatter is all body."""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    fields = {}
    key = None
    for raw in m.group(1).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")) or raw.lstrip().startswith("- "):
            if key is not None and raw.lstrip().startswith("- "):
                if not isinstance(fields.get(key), list):
                    fields[key] = []
                fields[key].append(_unquote(raw.lstrip()[2:]))
            elif key is not None and isinstance(fields.get(key), str):
                fields[key] = (fields[key] + " " + raw.strip()).strip()
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
        elif value in ("|", ">", "|-", ">-"):
            fields[key] = ""
        else:
            fields[key] = _unquote(value)
    return fields, text[m.end():]


def _unquote(s):
    s = str(s).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _display_path(path):
    base = _cwd or os.getcwd()
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def _first_clause(text, limit):
    text = " ".join(str(text or "").split())
    cut = re.split(r"[.;:(\n]| - | — ", text, maxsplit=1)[0].strip()
    if len(cut) > limit:
        cut = cut[:limit].rsplit(" ", 1)[0].rstrip(",") + "…"
    return cut[:1].lower() + cut[1:] if cut else ""


def _tokens(text):
    if _count_tokens is not None:
        try:
            return int(_count_tokens(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


# ── What the model sees ───────────────────────────────────────────────

def visible():
    """The skills the catalog names and the picker may choose: not hidden."""
    return [s for s in SKILLS.values() if not s.hidden]


def catalog_line():
    """One static line naming the skills, in discovery order (the same bytes every
    request). Empty when there are none."""
    items = [f"`{s.name}` ({s.short()})" if s.short() else f"`{s.name}`" for s in visible()]
    if not items:
        return ""
    return f"{CATALOG_PREFIX} {', '.join(items)}."


def catalog_text():
    """For a tool_search answer naming an unknown skill: what there is to load."""
    names = [s.name for s in visible()]
    return ("Skills available: " + ", ".join(names) + ".") if names else "No skills are configured."


def render(name):
    s = SKILLS.get(name)
    return s.render() if s else None


def resolve(name):
    """The skill a name means: exact, or case-insensitive; None if neither."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip()
    if n in SKILLS:
        return n
    low = n.lower()
    for k in SKILLS:
        if k.lower() == low:
            return k
    return None


def search(words):
    """Skills whose name, description or keywords carry any of the query words."""
    words = {w for w in re.findall(r"[a-z0-9]+", str(words or "").lower()) if len(w) > 2}
    if not words:
        return []
    hits = []
    for s in visible():
        hay = {w for w in re.findall(r"[a-z0-9]+", (s.name + " " + s.description + " "
                                                   + " ".join(s.keywords)).lower())}
        if words & hay:
            hits.append(s.name)
    return hits


# ── Loaded state ──────────────────────────────────────────────────────

def is_loaded(name):
    return name in LOADED


def loaded_tokens():
    return sum(SKILLS[n].tokens for n in LOADED if n in SKILLS)


def skills_of(msg):
    """The skills a history message carries: a tool_search result (every header line
    in it: `skill:A,B` answers with several, a notice may come first), any other
    message that begins with the header, or one a turn boundary appended skills to
    (`_skills`). Prose that merely mentions a header is not a load: only a tool_search
    result is scanned past its first line."""
    names = []
    content = msg.get("content")
    if isinstance(content, str):
        if msg.get("role") == "tool" and msg.get("name") == "tool_search":
            names += [m.group(1) for m in _HEADER_LINE_RE.finditer(content)]
        elif content.startswith(HEADER_PREFIX):
            m = _HEADER_RE.match(content)
            if m:
                names.append(m.group(1))
    for n in msg.get("_skills") or []:
        if isinstance(n, str) and n not in names:
            names.append(n)
    return list(dict.fromkeys(names))


def sync_loaded(conversation_history, oldest_idx):
    """LOADED is what the window carries: names whose text sits at or after the
    window's oldest message. A skill trimmed away leaves the list, so it can be loaded
    again. Returns the names that left."""
    before = list(LOADED)
    now = skills_in(conversation_history[max(0, int(oldest_idx or 0)):])
    LOADED[:] = now
    del RESUME_LOADED[:]
    return [n for n in before if n not in now]


def skills_in(messages):
    """The skills whose text is among these messages, in order (the summarizer)."""
    found = []
    for msg in messages:
        for n in skills_of(msg):
            if n not in found:
                found.append(n)
    return found


def can_load(name, extra=0):
    """(ok, reason): whether a skill may be loaded now."""
    s = SKILLS.get(name)
    if s is None:
        return False, f"no skill named {name}. {catalog_text()}"
    if name in LOADED or name in RESUME_LOADED:
        return False, f"skill {name} is already loaded (its text is in the conversation above)."
    if any(n == name for n, _ in PENDING):
        return False, f"skill {name} is already queued for the next turn."
    cap = CFG["max_loaded_tokens"]
    if cap and loaded_tokens() + extra + s.tokens > cap:
        have = ", ".join(LOADED) or "nothing"
        return False, (f"skill {name} ({s.tokens} tokens) would pass skills.max_loaded_tokens "
                       f"({cap}); loaded now: {have}. It can be loaded once those leave the window.")
    return True, ""


def load_text(names, by="model"):
    """For tool_search skill:NAME[,NAME]: the rendered text of each skill that can be
    loaded, with a notice for each that cannot. The text lands as the tool result, so
    the window carries it from now; LOADED notes it at once."""
    out = []
    tokens = 0
    loaded_here = 0
    for raw in names:
        name = resolve(raw) or raw
        ok, why = can_load(name, extra=tokens)
        if ok and SKILLS[name].hidden and by == "model":
            ok, why = False, (f"skill {name} is not available to the model (disable-model-invocation); "
                              "only /skill " + name + " at the keyboard loads it.")
        chars = sum(len(x) + 1 for x in out)   # notices count: the whole answer must not spill
        if ok and loaded_here and chars + len(SKILLS[name].render()) > CFG["max_chars"]:
            ok, why = False, (f"skill {name} does not fit in this answer beside the others; "
                              "load it with its own tool_search skill:" + name + " call.")
        if not ok:
            out.append(why)
            continue
        s = SKILLS[name]
        out.append(s.render())
        tokens += s.tokens
        loaded_here += 1
        LOADED.append(name)
        _note_load(name, by)
    return "\n".join(out)


def request_load(name, by="command"):
    """Queue a skill for the next turn boundary (the keyboard, the picker). Returns
    (ok, message)."""
    r = resolve(name)
    ok, why = can_load(r or name)
    if not ok:
        return False, why
    PENDING.append((r, by))
    return True, f"skill {r} loads at the next turn ({SKILLS[r].tokens} tokens)."


def take_pending():
    """Drain PENDING: [(name, rendered_text, by)] for the turn boundary."""
    got = []
    for name, by in PENDING:
        if name in SKILLS and name not in LOADED:
            got.append((name, SKILLS[name].render(), by))
            LOADED.append(name)
            _note_load(name, by)
    del PENDING[:]
    return got


def take_suggestions():
    """The runtime-note line for the picker's suggestions, once; '' when none."""
    names = [n for n in SUGGEST if n in SKILLS and n not in LOADED]
    del SUGGEST[:]
    if not names:
        return ""
    parts = [f"`{n}` ({SKILLS[n].short()})" if SKILLS[n].short() else f"`{n}`" for n in names]
    return ("A skill may apply: " + ", ".join(parts) + "; load it with tool_search skill:"
            + ",".join(names) + ".")


def _note_load(name, by):
    s = SKILLS.get(name)
    _log.info("skill.load name=%s tokens=%d by=%s", name, s.tokens if s else 0, by)
    _event("skill_load", by)


def _event(name, kind):
    try:
        import telemetry
        telemetry.record_patch_event(name, kind=kind)
    except Exception:
        pass


# ── The keyword floor ─────────────────────────────────────────────────

def task_text(text):
    """The user's own words in a message: before the blocks the agent appends and
    before any attached file (`[path: N lines]`), cut to the picker's cap."""
    try:
        from tools import own_text
        text = own_text(text)
    except Exception:
        text = str(text or "")
    m = re.search(r"\n\n\[[^\]\n]+: (?:first \d+ of )?\d+ lines\]\n", text)
    if m:
        text = text[:m.start()]
    return text.strip()[:_PICK_TEXT_CHARS]


def keyword_matches(text):
    """Skills a keyword phrase of which appears in the text as whole words."""
    low = " ".join(str(text or "").lower().split())
    hits = []
    for s in visible():
        if s.name in LOADED:
            continue
        for kw in s.keywords:
            if re.search(r"(?<![a-z0-9_])" + re.escape(kw) + r"(?![a-z0-9_])", low):
                hits.append(s.name)
                break
    return hits


# ── The picker ────────────────────────────────────────────────────────

def build_pick_prompt(text, candidates):
    lines = [f"- {s.name}: {s.description[:_PICK_DESC_CHARS]}" for s in candidates]
    return (
        "An agent is about to work on a task. Skills are instruction files it can load; "
        "each is a few thousand tokens, so load only one that clearly applies to the task "
        "as stated, not one that might be useful later.\n\n"
        "Skills available:\n" + "\n".join(lines) + "\n\n"
        "The task, in the user's words:\n" + text.strip() + "\n\n"
        "Answer with one JSON array and nothing else: for each skill that applies, "
        '{"name": "<skill name>", "confidence": <0 to 1>}; an empty array [] when none does.'
    )


def parse_pick(answer, candidates):
    """[(name, confidence)] from the judge's text, best first; None when unreadable."""
    s = str(answer or "")
    start, end = s.find("["), s.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except ValueError:
        return None
    if not isinstance(obj, list):
        return None
    allowed = {c.name for c in candidates}
    picks = {}
    for item in obj:
        if not isinstance(item, dict):
            continue
        name = resolve(str(item.get("name", "")))
        if name not in allowed:
            continue
        try:
            conf = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        conf = min(1.0, max(0.0, conf))
        picks[name] = max(conf, picks.get(name, 0.0))
    return sorted(picks.items(), key=lambda kv: -kv[1])


def pick(text, source="start", complete=None, timeout=None):
    """One judge call: the task text against the catalog. Returns a verdict dict
    {"source", "picks": [(name, conf)], "how": judge|failopen|none, "ms"}. Never raises."""
    complete = complete or _complete
    timeout = CFG["pick_timeout_s"] if timeout is None else timeout
    candidates = [s for s in visible() if s.name not in LOADED]
    text = str(text or "").strip()
    if not candidates or not text:
        return {"source": source, "picks": [], "how": "none", "ms": 0, "err": ""}
    if complete is None:
        return {"source": source, "picks": [], "how": "failopen", "ms": 0, "err": "no judge"}
    t0 = time.monotonic()
    try:
        answer = complete(build_pick_prompt(text, candidates), timeout)
    except Exception as e:  # noqa: BLE001 — the picker fails open on any judge error
        return {"source": source, "picks": [], "how": "failopen",
                "ms": int((time.monotonic() - t0) * 1000), "err": type(e).__name__}
    ms = int((time.monotonic() - t0) * 1000)
    picks = parse_pick(answer, candidates)
    if picks is None:
        return {"source": source, "picks": [], "how": "failopen", "ms": ms, "err": "unparseable"}
    return {"source": source, "picks": picks, "how": "judge", "ms": ms, "err": ""}


def apply_verdict(verdict):
    """Thresholds: queue loads (at most max_picks_per_turn) and suggestions; log the
    verdict. Returns (loaded_names, suggested_names)."""
    loads, suggests = [], []
    for name, conf in verdict.get("picks") or []:
        if name in LOADED or any(n == name for n, _ in PENDING):
            continue
        if conf >= CFG["load_threshold"] and len(loads) < CFG["max_picks_per_turn"]:
            ok, _why = request_load(name, by="picker")
            if ok:
                loads.append(name)
        elif conf >= CFG["suggest_threshold"] and name not in SUGGEST:
            SUGGEST.append(name)
            suggests.append(name)
    err = verdict.get("err") or ""
    _log.info("skill.pick source=%s how=%s picked=%s suggested=%s ms=%d%s",
              verdict.get("source"), verdict.get("how"), ",".join(loads) or "-",
              ",".join(suggests) or "-", verdict.get("ms", 0), f" err={err}" if err else "")
    _event("skill_pick", f"{verdict.get('source')}:{verdict.get('how')}")
    return loads, suggests


def pick_at_start(text):
    """The session-start pick, synchronous (nothing is cached yet, so a load here is
    free of misses): the judge when `pick` is on, the keyword floor when it is off or
    the judge failed. Returns (loaded, suggested)."""
    STATE["started"] = True
    text = task_text(text)
    if not SKILLS or not text:
        return [], []
    if CFG["pick"] != "off":
        v = pick(text, source="start")
        loads, suggests = apply_verdict(v)
        if v["how"] == "judge":
            return loads, suggests
    loads = []
    for name in keyword_matches(text):
        ok, _why = request_load(name, by="keywords")
        if ok:
            loads.append(name)
    if loads:
        _log.info("skill.pick source=start how=keywords picked=%s suggested=- ms=0", ",".join(loads))
        _event("skill_pick", "start:keywords")
    return loads, []


class Picker:
    """The asynchronous pick for later messages (`pick: "always"`): one worker thread
    per kick, the newest verdict kept until a turn boundary harvests it; a verdict
    overtaken by a newer kick is dropped."""

    def __init__(self):
        self._lock = threading.Lock()
        self._gen = 0
        self._result = None

    def kick(self, text, source):
        text = task_text(text)
        if not text or CFG["pick"] != "always" or not any(
                s.name not in LOADED for s in visible()):
            return False
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._result = None

        def _run():
            v = pick(text, source=source)
            with self._lock:
                if gen == self._gen:
                    self._result = v
                else:
                    _log.info("skill.pick source=%s how=dropped (a newer message arrived)", source)
        threading.Thread(target=_run, name="skill-picker", daemon=True).start()
        return True

    def harvest(self):
        with self._lock:
            v, self._result = self._result, None
        return v

    def reset(self):
        with self._lock:
            self._gen += 1
            self._result = None


PICKER = Picker()


def reset_session():
    """Forget the session's loads and verdicts (tests, /clear)."""
    del LOADED[:]
    del PENDING[:]
    del SUGGEST[:]
    del RESUME_LOADED[:]
    STATE["started"] = False
    PICKER.reset()
