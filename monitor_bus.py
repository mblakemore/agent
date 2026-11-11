"""Background monitor bus — generic watchers that inject into the agent loop.

A *monitor* runs a shell command on a fixed interval inside a daemon thread.
When the command emits non-empty stdout, that output is queued on a shared
bus. The agentic loop drains the bus at each turn-complete boundary and feeds
queued items into the conversation as new user turns — so a monitor can pop
new work in "as the agent continues".

Design guarantees:
  * Generic: a monitor watches ANY shell command. No coupling to any data
    source — the caller supplies the command.
  * Daemon threads: monitors die with the process. An -a cycle that ends still
    exits promptly; a monitor never extends process lifetime, and stop_all()
    never block-joins (joining a monitor mid-subprocess would stall exit).
  * Bounded: per-poll subprocess timeout <= interval; output length capped.

Armed via the `monitor` tool (tools/monitor.py); drained by agent.py.

Sources, the gate and `wait` (2026-11): everything the agent is tracking is a *source*
on this one bus: a monitor (a command on an interval, or a watched file), a background
exec session, a background subagent. A finished session or subagent puts one item on the
bus by itself, so the common case is one wake and no polling. A source may carry a
*brief* (gate.py): then its output is judged by the summary model and only a "wake"
verdict is enqueued; "hold" verdicts accumulate as a digest that rides on the next wake.
`wait()` blocks a turn, for free, until anything tracked produces an item (or the
listed sources do), and returns everything that is ready; typed input interrupts it.
"""

import json
import logging
import os
import queue as _queue
import secrets
import signal
import subprocess
import threading
import time

import gate as _gate

_log = logging.getLogger("agent")

_MAX_OUTPUT_CHARS = 4000    # cap one injection to protect the context window
_DEFAULT_INTERVAL = 15.0
_MIN_INTERVAL = 2.0
_MAX_MONITORS = 8           # sanity ceiling on concurrent monitors
_WATCH_READ_CAP = 64 * 1024  # bytes read from a watched file per poll
_FINISHED_TAIL_CHARS = 1500  # output tail carried by a "finished" item
_SWEEP_S = 5.0               # how often aged held digests are checked
_REAP_DONE_S = 900.0         # a finished source is forgotten this long after its item

# Harness configuration (agent.py calls configure(); the defaults stand alone).
_CFG = {"session_events": True, "gate": _gate.config_from(None), "judge": None}


def _kill_group(proc):
    """SIGKILL the process GROUP of proc, then reap. Never raises.

    Killing only the direct child leaks grandchildren: `bash -c 'tail -f x |
    grep y'` SIGKILLed at the bash level leaves tail running forever (verified
    live 2026-08-07 — two survivors from one timed-out poll). Because each
    poll starts its own session (start_new_session=True below), the group id
    is the child pid and killpg takes the whole pipeline down."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _run_once(command, timeout, register=None):
    """Run command via `bash -c` in its OWN PROCESS GROUP. Return stripped
    stdout on success (may be ''), or None on non-zero exit / timeout / error.
    Never raises.

    `register` (optional callable) receives the live Popen (and then None when
    the poll ends) so the owning monitor can group-kill an in-flight child at
    stop()/exit — otherwise 'exit' orphans whatever the poll was running and a
    persistent-style command survives the agent (the 15-day stale process
    class, 2026-08-07)."""
    p = None
    try:
        p = subprocess.Popen(["bash", "-c", command],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        if register:
            register(p)
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log.debug("monitor cmd timed out after %ss — killing group", timeout)
        _kill_group(p)
        return None
    except Exception as e:
        _log.debug("monitor cmd errored: %s", e)
        _kill_group(p)
        return None
    finally:
        if register:
            register(None)
    if p.returncode != 0:
        _log.debug("monitor cmd exit=%s: %s", p.returncode,
                   ((err or "")[-200:]).strip())
        return None
    return (out or "").strip()


class _Monitor:
    def __init__(self, label, command, interval, cmd_timeout, prefix, dedup, path=None):
        self.label = label
        self.command = command
        self.path = path                      # a watched file instead of a command
        self.interval = interval
        self.cmd_timeout = cmd_timeout
        self.prefix = prefix
        self.dedup = dedup
        self._last = None
        self._offset = None                   # watch: bytes already seen
        self._stop = threading.Event()
        self._proc = None                     # live Popen while a poll runs
        self._proc_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name=f"monitor:{label}", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        """Signal the loop AND group-kill any in-flight child. Without the
        kill, 'exit' (or a monitor replace) orphans the running poll — a
        daemon thread dies with the process but its subprocess does not."""
        self._stop.set()
        with self._proc_lock:
            proc, self._proc = self._proc, None
        _kill_group(proc)

    def _register(self, proc):
        with self._proc_lock:
            self._proc = proc

    def _read_new(self):
        """Watch: the bytes appended to the file since the last poll (the file's current
        size on the first poll, so an existing log is not replayed). Truncation restarts
        from the top. '' when nothing is new, None when the file is unreadable."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return None
        if self._offset is None:
            self._offset = size
            return ""
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return ""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read(_WATCH_READ_CAP)
        except OSError:
            return None
        self._offset += len(data)
        return data.decode("utf-8", errors="replace").strip()

    def _run(self):
        while not self._stop.is_set():
            if self.path:
                out = self._read_new()
            else:
                out = _run_once(self.command, self.cmd_timeout, register=self._register)
            if self._stop.is_set():
                break  # stopped during the poll — don't inject a straggler
            if out and not (self.dedup and out == self._last):
                self._last = out
                if len(out) > _MAX_OUTPUT_CHARS:
                    out = out[:_MAX_OUTPUT_CHARS] + "\n…[truncated]"
                _BUS.emit(self.label, out, prefix=self.prefix)
            # Interruptible sleep — returns immediately when stop() is called.
            self._stop.wait(self.interval)


class Source:
    """One thing the agent is tracking. `held` is [(ts, line, raw)] the gate kept back."""

    def __init__(self, sid, kind, label=None, brief="", bypass="", quiet="", note=""):
        self.id = sid
        self.kind = kind                      # monitor | watch | session | subagent
        self.label = label or sid
        self.note = note                      # the command, path or prompt, for listings
        self.set_gate(brief, bypass, quiet)
        self.held = []
        self.held_chars = 0
        self.events = 0                       # items this source has put on the bus
        self.done = False                     # sessions and subagents: finished
        self.started = time.time()
        self.result = ""                      # the finished item's text, re-readable

    def set_gate(self, brief="", bypass="", quiet=""):
        self.brief = str(brief or "").strip()
        self.bypass_src, self.quiet_src = str(bypass or "").strip(), str(quiet or "").strip()
        self.bypass = _gate.compile_pattern(self.bypass_src)
        self.quiet = _gate.compile_pattern(self.quiet_src)

    @property
    def gated(self):
        return bool(self.brief or self.bypass is not None or self.quiet is not None)

    def describe(self):
        age = int(time.time() - self.started)
        state = "done" if self.done else ("running" if self.kind in ("session", "subagent")
                                          else "watching")
        held = f", {len(self.held)} held" if self.held else ""
        note = f" `{self.note[:60]}`" if self.note else ""
        return f"{self.id} ({self.kind}, {state}, {age // 60} min{held}){note}"


def _persist_path():
    """Where persistent monitors are saved: AGENT_MONITORS_FILE, else
    .agent/monitors.json under the working directory."""
    return os.environ.get("AGENT_MONITORS_FILE") or os.path.join(".agent", "monitors.json")


def _load_persisted():
    try:
        with open(_persist_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_persisted(specs):
    path = _persist_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(specs, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        _log.warning("could not save persistent monitors to %s: %s", path, e)


class _Bus:
    def __init__(self):
        self._q = _queue.Queue()
        self._monitors = {}
        self._lock = threading.Lock()
        self._sources = {}
        self._cv = threading.Condition()      # notified on every enqueue / source change
        self._sweeper = None

    # ── sources ───────────────────────────────────────────────────────────
    def _add_source(self, src):
        with self._lock:
            self._sources[src.id] = src
        self._ensure_sweeper()
        with self._cv:
            self._cv.notify_all()
        return src

    def source(self, sid):
        with self._lock:
            return self._sources.get(str(sid or "").strip())

    def _remove_source(self, sid):
        with self._lock:
            src = self._sources.pop(sid, None)
        if src is not None and src.held:
            self._flush_held(src, reason="source removed")
        return src

    def list_sources(self, live_only=False):
        with self._lock:
            srcs = list(self._sources.values())
        if live_only:
            srcs = [x for x in srcs if not x.done]
        return srcs

    def set_gate(self, sid, brief=None, bypass=None, quiet=None):
        src = self.source(sid)
        if src is None:
            return {"ok": False, "error": f"no source '{sid}'"}
        src.set_gate(src.brief if brief is None else brief,
                     src.bypass_src if bypass is None else bypass,
                     src.quiet_src if quiet is None else quiet)
        if src.gated:
            self._ensure_sweeper()
        self._persist_gate(src)
        return {"ok": True, "id": src.id, "gated": src.gated}

    def _persist_gate(self, src):
        """A persisted monitor keeps its brief and patterns across restarts."""
        if src.kind not in ("monitor", "watch"):
            return
        specs = _load_persisted()
        sp = specs.get(src.id)
        if isinstance(sp, dict):
            sp["brief"], sp["bypass"], sp["quiet"] = src.brief, src.bypass_src, src.quiet_src
            _save_persisted(specs)

    # ── emit: a source's output, through the gate when it has one ────────────
    def emit(self, sid, text, prefix=""):
        """Output from source `sid`: enqueued, or judged first when the source is gated."""
        src = self.source(sid)
        if src is None or not src.gated:
            self._put(prefix + text, source=src)
            return _gate.Verdict(_gate.WAKE, "not gated", how="bypass")
        cfg = _CFG["gate"]
        with self._lock:
            held_lines = [h[1] for h in src.held]
        v = _gate.judge(src.brief, src.label, text, held_lines, bypass=src.bypass,
                        quiet=src.quiet, complete=_CFG["judge"], timeout=cfg["timeout_s"])
        _record_verdict(v)
        if v.verdict == _gate.WAKE:
            self._flush_held(src, reason="woken")
            self._put(prefix + text, source=src)
            return v
        path = _gate.append_raw(cfg["log_dir"], src.label, text)
        with self._lock:
            src.held.append((time.time(), v.line, text))
            src.held_chars += len(text)
            src.raw_path = path
            over = cfg["max_held_chars"] and src.held_chars > cfg["max_held_chars"]
        if over:
            self._flush_held(src, reason="held buffer full")
        return v

    def _take_held(self, src, reason=""):
        with self._lock:
            held, src.held, src.held_chars = src.held, [], 0
        if not held:
            return ""
        cfg = _CFG["gate"]
        return _gate.format_digest(src.label, held, raw_chars=cfg["digest_raw_chars"],
                                   log_path=getattr(src, "raw_path", None), reason=reason)

    def _flush_held(self, src, reason=""):
        """Held items become one digest item on the bus (a deliberate wake)."""
        text = self._take_held(src, reason)
        if text:
            self._put(text, source=src, count=False)

    def take_held_digests(self):
        """Every source's held items as digest texts, emptying them. For a wake that is
        already happening: the digests travel on it instead of causing one."""
        out = []
        for src in self.list_sources():
            text = self._take_held(src, reason="rode along")
            if text:
                out.append(text)
        return out

    def _ensure_sweeper(self):
        with self._lock:
            if self._sweeper is not None and self._sweeper.is_alive():
                return
            self._sweeper = threading.Thread(target=self._sweep, name="gate-sweeper",
                                             daemon=True)
            self._sweeper.start()

    def _sweep(self):
        """Flush held items older than digest_after_s; reap finished sources after a while."""
        while True:
            time.sleep(_SWEEP_S)
            try:
                after = _CFG["gate"]["digest_after_s"]
                now = time.time()
                for src in self.list_sources():
                    with self._lock:
                        oldest = src.held[0][0] if src.held else None
                    if oldest is not None and after and now - oldest > after:
                        self._flush_held(src, reason=f"held over {int(after)}s")
                    # A finished session or subagent nobody waited on is forgotten
                    # after a while; its item was delivered at a turn boundary.
                    if src.done and now - getattr(src, "finished_at", now) > _REAP_DONE_S:
                        self._remove_source(src.id)
            except Exception:  # noqa: BLE001 — a sweeper must never die
                _log.debug("gate sweeper", exc_info=True)

    # ── sessions and subagents ───────────────────────────────────────────────
    def source_started(self, sid, kind, note="", label=None):
        """A background session or subagent began: it is tracked from now on."""
        src = Source(str(sid), kind, label=label, note=note)
        return self._add_source(src)

    def source_finished(self, sid, text):
        """A background session or subagent ended: one item on the bus (unless
        session_events is off), the source stays listed as done until it is reaped."""
        src = self.source(sid)
        if src is None:
            src = self._add_source(Source(str(sid), "session"))
        src.done = True
        src.finished_at = time.time()
        src.result = text
        if _CFG["session_events"]:
            self._put(text, source=src)
        else:
            with self._cv:
                self._cv.notify_all()
        return src

    def forget(self, sid):
        """Stop tracking a finished session or subagent (its item has been read)."""
        return self._remove_source(str(sid or "").strip())

    # ── wait ──────────────────────────────────────────────────────────────────
    def wait(self, any_of=None, all_of=None, max_s=600.0, cancel_check=None):
        """Block until something tracked happens; return what is ready.

        No filters: the first enqueue ends it. `any_of`: an item (or completion) from one
        of those sources. `all_of`: every listed source done. A typed line ends it at
        once (re-enqueued for the loop) with interrupted="user input". Items that arrive
        meanwhile are collected and returned, since they would be delivered next anyway.
        Returns {"items": [text], "elapsed": s, "interrupted": str|None, "reason": str}.
        """
        any_of = [str(x).strip() for x in (any_of or []) if str(x).strip()]
        all_of = [str(x).strip() for x in (all_of or []) if str(x).strip()]
        start = time.monotonic()
        deadline = start + max(0.0, float(max_s))
        snapshot = {x.id: x.events for x in self.list_sources()}
        items, interrupted, reason = [], None, "timeout"

        def _any_fired():
            for sid in any_of:
                src = self.source(sid)
                if src is None or src.done or src.events > snapshot.get(sid, 0):
                    return True
            return False

        def _all_done():
            return all((self.source(sid) is None or self.source(sid).done) for sid in all_of)

        while True:
            drained = self.drain_items(with_digests=False)
            for kind, text in drained:
                if kind == "user":
                    self.put_user(text)
                    interrupted = "user input"
                else:
                    items.append(text)
            if interrupted:
                reason = "interrupted"
                break
            if all_of:
                if _all_done():
                    reason = "all done"
                    break
            elif any_of:
                if _any_fired():
                    reason = "ready"
                    break
            elif items:
                reason = "ready"
                break
            if cancel_check is not None and cancel_check():
                reason = "cancelled"
                break
            now = time.monotonic()
            if now >= deadline:
                break
            with self._cv:
                self._cv.wait(min(0.25, deadline - now))
        if reason in ("ready", "all done"):
            # A moment for whatever else was about to land, so one wake carries it all.
            time.sleep(0.1)
            for kind, text in self.drain_items(with_digests=False):
                if kind == "user":
                    self.put_user(text)
                    interrupted = interrupted or "user input"
                else:
                    items.append(text)
        items += self.take_held_digests() if items else []
        return {"items": items, "elapsed": time.monotonic() - start,
                "interrupted": interrupted, "reason": reason}

    def arm(self, label, command, interval=_DEFAULT_INTERVAL, cmd_timeout=None,
            prefix="", dedup=True, persist=False, path=None, brief="", bypass="", quiet=""):
        label = str(label or "").strip()
        command = str(command or "").strip()
        path = str(path or "").strip() or None
        if not label:
            return {"ok": False, "error": "label required"}
        if not command and not path:
            return {"ok": False, "error": "command (or path) required"}
        try:
            interval = float(interval)
        except Exception:
            interval = _DEFAULT_INTERVAL
        interval = max(_MIN_INTERVAL, interval)
        try:
            cmd_timeout = float(cmd_timeout) if cmd_timeout is not None else min(10.0, interval)
        except Exception:
            cmd_timeout = min(10.0, interval)
        # C4107 discipline: a hung poll can never wedge the loop past one interval.
        cmd_timeout = min(cmd_timeout, interval)
        with self._lock:
            existing = self._monitors.get(label)
            if existing is None and len(self._monitors) >= _MAX_MONITORS:
                return {"ok": False, "error": f"too many monitors (max {_MAX_MONITORS})"}
            if existing is not None:
                existing.stop()   # replace: stop the old one, start a fresh one
            m = _Monitor(label, command, interval, cmd_timeout, prefix, bool(dedup), path=path)
            self._monitors[label] = m
            self._sources[label] = Source(label, "watch" if path else "monitor",
                                          brief=brief, bypass=bypass, quiet=quiet,
                                          note=path or command)
            m.start()
        if self._sources[label].gated:
            self._ensure_sweeper()
        # persist=True saves the spec so restore_persisted() re-arms it after a
        # restart; persist=False on an existing persistent label un-persists it.
        specs = _load_persisted()
        if persist:
            specs[label] = {"command": command, "path": path, "interval": interval,
                            "prefix": prefix, "dedup": bool(dedup),
                            "brief": str(brief or ""), "bypass": str(bypass or ""),
                            "quiet": str(quiet or "")}
            _save_persisted(specs)
        elif label in specs:
            specs.pop(label)
            _save_persisted(specs)
        return {"ok": True, "label": label, "interval": interval, "gated": self._sources[label].gated,
                "replaced": existing is not None, "persisted": bool(persist)}

    def restore_persisted(self):
        """Re-arm every saved persistent monitor. Returns the labels armed."""
        armed = []
        for label, sp in _load_persisted().items():
            if not isinstance(sp, dict) or label in self._monitors:
                continue
            r = self.arm(label, sp.get("command"), interval=sp.get("interval", _DEFAULT_INTERVAL),
                         prefix=sp.get("prefix", ""), dedup=sp.get("dedup", True), persist=True,
                         path=sp.get("path"), brief=sp.get("brief", ""),
                         bypass=sp.get("bypass", ""), quiet=sp.get("quiet", ""))
            if r.get("ok"):
                armed.append(label)
        return armed

    def disarm(self, label):
        label = str(label or "").strip()
        with self._lock:
            m = self._monitors.pop(label, None)
        if m is None:
            return {"ok": False, "error": f"no monitor '{label}'"}
        m.stop()
        self._remove_source(label)
        specs = _load_persisted()
        if specs.pop(label, None) is not None:
            _save_persisted(specs)
        return {"ok": True, "label": label}

    def list_active(self):
        with self._lock:
            return [{"label": m.label, "command": m.command or m.path, "interval": m.interval,
                     "gated": bool(self._sources.get(m.label) and self._sources[m.label].gated)}
                    for m in self._monitors.values()]

    def _put(self, text, kind="monitor", source=None, count=True):
        # Items are (kind, text) tuples so consumers can distinguish a typed
        # user line (needs slash/exit/file-ref handling) from a monitor ping
        # (injected raw). drain() flattens to text for the mid-burst path,
        # which treats both identically; get_blocking() preserves the tuple.
        if source is not None and count:
            source.events += 1
        self._q.put((kind, text))
        with self._cv:
            self._cv.notify_all()
        _fire_notifier()   # wake an idle interactive prompt, if one registered

    def put_user(self, text):
        """Enqueue a typed user line (provenance 'user')."""
        self._put(text, kind="user")

    def get_blocking(self, timeout=None):
        """Block for the next (kind, text) item; return None on timeout.
        Used by the concurrent-input main loop as its idle wait."""
        try:
            return self._q.get(block=True, timeout=timeout)
        except _queue.Empty:
            return None

    def has_pending(self):
        # Best-effort: is anything queued right now? Used by the interactive
        # loop to drain before blocking on input (closes the arrived-just-
        # before-prompt race).
        return not self._q.empty()

    def drain_items(self, with_digests=True):
        """Return the queued (kind, text) items (FIFO), emptying the queue. A raw
        text item counts as a monitor ping. Non-blocking; safe every turn.
        When anything was queued, the gate's held digests ride along after it."""
        out = []
        while True:
            try:
                item = self._q.get_nowait()
            except _queue.Empty:
                break
            out.append(item if isinstance(item, tuple) else ("monitor", item))
        if out and with_digests:
            out += [("monitor", d) for d in self.take_held_digests()]
        return out

    def drain(self):
        """Return queued injection TEXTS (FIFO), emptying the queue. Flattens
        (kind, text) tuples to text for callers that treat a typed user line
        and a monitor ping identically (the agent loop's own drain keeps the
        kind, see `drain_items`). Non-blocking; safe every turn."""
        return [text for _kind, text in self.drain_items()]

    def stop_all(self):
        """Signal every monitor to stop. Does NOT join — daemon threads die
        with the process; joining one mid-subprocess would stall exit."""
        with self._lock:
            monitors = list(self._monitors.values())
            self._monitors.clear()
        for m in monitors:
            m.stop()
        with self._lock:
            self._sources.clear()


_BUS = _Bus()


def _record_verdict(v):
    try:
        import telemetry
        telemetry.record_patch_event("gate", kind=f"{v.verdict}:{v.how}")
    except Exception:  # noqa: BLE001 — telemetry is optional
        pass


_KEEP = object()


def configure(session_events=None, gate=None, judge=_KEEP):
    """Harness wiring (agent.py at startup): whether finished sessions put an item on the
    bus, the `preferences.gate` dict, and `judge(prompt, timeout) -> str` (the summary
    model; None means no judge, so every gated source wakes). An argument left out keeps
    its current value."""
    if session_events is not None:
        _CFG["session_events"] = bool(session_events)
    if gate is not None:
        _CFG["gate"] = _gate.config_from(gate)
    if judge is not _KEEP:
        _CFG["judge"] = judge
    return dict(_CFG)


def gate_enabled():
    return bool(_CFG["gate"]["enabled"])


def gate_config():
    return dict(_CFG["gate"])


def new_id(prefix):
    return f"{prefix}-{secrets.token_hex(3)}"

# Optional notifier: a zero-arg callback fired (best-effort, from the monitor
# thread) whenever new output is queued. The interactive front-end registers
# one so an idle prompt can wake immediately instead of waiting for the next
# turn. Default None -> no-op (so -a / non-interactive paths are unaffected).
_notifier = None
_notifier_lock = threading.Lock()


def set_notifier(fn):
    """Register a zero-arg callback fired when output is queued (or None)."""
    global _notifier
    with _notifier_lock:
        _notifier = fn


def clear_notifier():
    set_notifier(None)


def _fire_notifier():
    with _notifier_lock:
        fn = _notifier
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


def has_pending():
    return _BUS.has_pending()


def put_user(text):
    """Enqueue a typed user line (concurrent-input producer)."""
    return _BUS.put_user(text)


def get_blocking(timeout=None):
    """Block for the next (kind, text) item; None on timeout."""
    return _BUS.get_blocking(timeout=timeout)


# Module-level convenience API (agent.py + tools/monitor.py call these).
def arm(label, command, **kw):
    return _BUS.arm(label, command, **kw)


def disarm(label):
    return _BUS.disarm(label)


def restore_persisted():
    return _BUS.restore_persisted()


def list_active():
    return _BUS.list_active()


def drain():
    return _BUS.drain()


def drain_items():
    """The queued (kind, text) items, emptying the queue."""
    return _BUS.drain_items()


def stop_all():
    return _BUS.stop_all()


def inject(text):
    """Queue raw text for the agent loop, as a monitor ping (used by schedule_bus)."""
    _BUS._put(text)


def emit(label, text, prefix=""):
    """Output from a source, through its gate when it has one."""
    return _BUS.emit(label, text, prefix=prefix)


def source_started(sid, kind, note="", label=None):
    return _BUS.source_started(sid, kind, note=note, label=label)


def source_finished(sid, text):
    return _BUS.source_finished(sid, text)


def forget(sid):
    return _BUS.forget(sid)


def source(sid):
    return _BUS.source(sid)


def list_sources(live_only=False):
    return _BUS.list_sources(live_only=live_only)


def set_gate(sid, brief=None, bypass=None, quiet=None):
    return _BUS.set_gate(sid, brief=brief, bypass=bypass, quiet=quiet)


def take_held_digests():
    return _BUS.take_held_digests()


def flush_held(sid=None):
    """Flush one source's held items (or every source's) onto the bus now."""
    srcs = [_BUS.source(sid)] if sid else _BUS.list_sources()
    n = 0
    for src in srcs:
        if src is not None and src.held:
            _BUS._flush_held(src, reason="flushed")
            n += 1
    return n


def wait(any_of=None, all_of=None, max_s=600.0, cancel_check=None):
    return _BUS.wait(any_of=any_of, all_of=all_of, max_s=max_s, cancel_check=cancel_check)
