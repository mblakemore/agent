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
"""

import logging
import queue as _queue
import subprocess
import threading

_log = logging.getLogger("agent")

_MAX_OUTPUT_CHARS = 4000    # cap one injection to protect the context window
_DEFAULT_INTERVAL = 15.0
_MIN_INTERVAL = 2.0
_MAX_MONITORS = 8           # sanity ceiling on concurrent monitors


def _run_once(command, timeout):
    """Run command via `bash -c`. Return stripped stdout on success (may be
    ''), or None on non-zero exit / timeout / error. Never raises."""
    try:
        p = subprocess.run(["bash", "-c", command], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log.debug("monitor cmd timed out after %ss", timeout)
        return None
    except Exception as e:
        _log.debug("monitor cmd errored: %s", e)
        return None
    if p.returncode != 0:
        _log.debug("monitor cmd exit=%s: %s", p.returncode,
                   ((p.stderr or "")[-200:]).strip())
        return None
    return (p.stdout or "").strip()


class _Monitor:
    def __init__(self, label, command, interval, cmd_timeout, prefix, dedup):
        self.label = label
        self.command = command
        self.interval = interval
        self.cmd_timeout = cmd_timeout
        self.prefix = prefix
        self.dedup = dedup
        self._last = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"monitor:{label}", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            out = _run_once(self.command, self.cmd_timeout)
            if self._stop.is_set():
                break  # stopped during the poll — don't inject a straggler
            if out and not (self.dedup and out == self._last):
                self._last = out
                if len(out) > _MAX_OUTPUT_CHARS:
                    out = out[:_MAX_OUTPUT_CHARS] + "\n…[truncated]"
                _BUS._put(self.prefix + out)
            # Interruptible sleep — returns immediately when stop() is called.
            self._stop.wait(self.interval)


class _Bus:
    def __init__(self):
        self._q = _queue.Queue()
        self._monitors = {}
        self._lock = threading.Lock()

    def arm(self, label, command, interval=_DEFAULT_INTERVAL, cmd_timeout=None,
            prefix="", dedup=True):
        label = str(label or "").strip()
        command = str(command or "").strip()
        if not label:
            return {"ok": False, "error": "label required"}
        if not command:
            return {"ok": False, "error": "command required"}
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
            m = _Monitor(label, command, interval, cmd_timeout, prefix, bool(dedup))
            self._monitors[label] = m
            m.start()
        return {"ok": True, "label": label, "interval": interval,
                "replaced": existing is not None}

    def disarm(self, label):
        label = str(label or "").strip()
        with self._lock:
            m = self._monitors.pop(label, None)
        if m is None:
            return {"ok": False, "error": f"no monitor '{label}'"}
        m.stop()
        return {"ok": True, "label": label}

    def list_active(self):
        with self._lock:
            return [{"label": m.label, "command": m.command, "interval": m.interval}
                    for m in self._monitors.values()]

    def _put(self, text):
        self._q.put(text)
        _fire_notifier()   # wake an idle interactive prompt, if one registered

    def has_pending(self):
        # Best-effort: is anything queued right now? Used by the interactive
        # loop to drain before blocking on input (closes the arrived-just-
        # before-prompt race).
        return not self._q.empty()

    def drain(self):
        """Return all queued injection strings (FIFO), emptying the queue.
        Non-blocking; safe to call every turn."""
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except _queue.Empty:
                break
        return out

    def stop_all(self):
        """Signal every monitor to stop. Does NOT join — daemon threads die
        with the process; joining one mid-subprocess would stall exit."""
        with self._lock:
            monitors = list(self._monitors.values())
            self._monitors.clear()
        for m in monitors:
            m.stop()


_BUS = _Bus()

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


# Module-level convenience API (agent.py + tools/monitor.py call these).
def arm(label, command, **kw):
    return _BUS.arm(label, command, **kw)


def disarm(label):
    return _BUS.disarm(label)


def list_active():
    return _BUS.list_active()


def drain():
    return _BUS.drain()


def stop_all():
    return _BUS.stop_all()
